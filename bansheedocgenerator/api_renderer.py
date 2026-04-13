"""API renderer: emits HTML pages for groups, classes, enums via Jinja2."""

from __future__ import annotations

import html
import re
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markdown_it import MarkdownIt

from .ir_builder import resolve_base_class, resolve_symbol
from .model import Class, Enum, FreeFunction, Group, Manual, Site
from .util import relative_link, safe_anchor, warn


_B3D_REF_RE = re.compile(r"@b3d::([A-Za-z_][\w]*(?:::[A-Za-z_~][\w]*)*)")


def _strip_b3d(name: str) -> str:
	"""Drop the implicit ``b3d::`` namespace prefix from a qualified name.
	Every symbol in the engine lives under this namespace, so displaying it
	only adds visual noise."""
	if name.startswith("b3d::"):
		return name[5:]
	return name

# C++ primitive / keyword tokens that are never symbols — do not try to resolve.
_NON_SYMBOL_TOKENS = {
	"void", "bool", "char", "short", "int", "long", "float", "double",
	"unsigned", "signed", "size_t", "ssize_t", "ptrdiff_t", "nullptr_t",
	"u8", "u16", "u32", "u64", "s8", "s16", "s32", "s64", "f32", "f64",
	"const", "volatile", "mutable", "constexpr", "consteval", "constinit",
	"static", "inline", "virtual", "explicit", "noexcept", "override", "final",
	"template", "typename", "class", "struct", "enum", "union", "namespace",
	"public", "protected", "private", "friend", "operator",
	"true", "false", "nullptr", "this", "return", "auto",
	"decltype", "sizeof", "typeid", "new", "delete",
	"true_type", "false_type",
}

# Regex that splits a rendered signature into alternating identifier and
# non-identifier runs so we can linkify qualified-name tokens in place.
_SIG_TOKEN_RE = re.compile(r"([A-Za-z_][\w]*(?:::[A-Za-z_][\w]*)*)")


def _compute_name_skip_ranges(signature: str) -> list[tuple[int, int]]:
	"""Identify offsets in a signature that are identifier-token positions which
	should NOT be linkified: the method/function name (last identifier before
	the first top-level '(') and each parameter name (last identifier in each
	comma-separated segment inside the parens)."""
	skips: list[tuple[int, int]] = []
	# Find the first top-level '(' (ignoring those inside angle brackets).
	angle = 0
	p_start = -1
	for i, c in enumerate(signature):
		if c == "<":
			angle += 1
		elif c == ">" and angle > 0:
			angle -= 1
		elif c == "(" and angle == 0:
			p_start = i
			break
	if p_start == -1:
		return skips
	# Method name: last identifier in signature[:p_start]
	head = signature[:p_start]
	matches = list(_SIG_TOKEN_RE.finditer(head))
	if matches:
		m = matches[-1]
		# Only skip simple identifiers, not qualified ones (unlikely here).
		if "::" not in m.group(1):
			skips.append(m.span())
	# Parameter names: within the top-level parentheses, split at top-level
	# commas and skip the last identifier in each segment.
	depth = 0
	seg_start = p_start + 1
	i = p_start + 1
	n = len(signature)
	p_end = -1
	segments: list[tuple[int, int]] = []
	while i < n:
		c = signature[i]
		if c == "(":
			depth += 1
		elif c == ")":
			if depth == 0:
				segments.append((seg_start, i))
				p_end = i
				break
			depth -= 1
		elif c == "," and depth == 0:
			segments.append((seg_start, i))
			seg_start = i + 1
		i += 1
	for s, e in segments:
		seg_text = signature[s:e]
		# Strip default value from consideration.
		eq = seg_text.find("=")
		if eq != -1:
			seg_text = seg_text[:eq]
			e = s + eq
		seg_matches = list(_SIG_TOKEN_RE.finditer(signature[s:e]))
		if seg_matches:
			m = seg_matches[-1]
			# Skip parameter name only if there's at least one preceding token
			# (the type). Otherwise (single token like `void`) leave it alone.
			if len(seg_matches) > 1:
				ms, me = m.span()
				skips.append((s + ms, s + me))
	return skips


_TRAILING_PREFIX_RE = re.compile(
	r"^(?:template\s*<.*?>\s+)?"
	r"(?:(?:static|virtual|constexpr|inline|explicit|friend)\s+)*",
	re.DOTALL,
)


def _rewrite_to_trailing_return(signature: str) -> str:
	"""Convert a signature like ``void Transform(const Matrix& m) const`` into
	``Transform(const Matrix& m) const -> void`` so the function name leads.
	Field signatures (no top-level ``(``) and constructors/destructors (no
	return type) are returned unchanged."""
	if not signature:
		return signature
	angle = 0
	p_start = -1
	for i, c in enumerate(signature):
		if c == "<":
			angle += 1
		elif c == ">" and angle > 0:
			angle -= 1
		elif c == "(" and angle == 0:
			p_start = i
			break
	if p_start == -1:
		return signature
	head = signature[:p_start]
	rest = signature[p_start:]
	m = _TRAILING_PREFIX_RE.match(head)
	prefix = head[: m.end()] if m else ""
	middle = head[m.end() :] if m else head
	m_name = re.search(r"([A-Za-z_~]\w*)\s*$", middle)
	if not m_name:
		return signature
	name = m_name.group(1)
	return_type = middle[: m_name.start()].strip()
	if not return_type:
		return signature
	prefix = prefix.rstrip()
	lead = f"{prefix} {name}" if prefix else name
	return f"{lead}{rest} -> {return_type}"


def _linkify_signature(
	signature: str,
	site: Site,
	current_url: str,
	link_types: bool = True,
) -> str:
	"""Render a signature string with resolvable identifier tokens linkified.

	Output is HTML — non-identifier pieces are HTML-escaped; identifier tokens
	that resolve via the symbol index are wrapped in an <a>. The method name
	itself and parameter names are not linkified. When ``link_types`` is False,
	type tokens are rendered as plain text (used inside TOC rows to avoid
	nested <a> elements — a row is itself wrapped in an anchor to the member).
	"""
	if not signature:
		return ""
	skip_ranges = _compute_name_skip_ranges(signature)
	def in_skip(start: int, end: int) -> bool:
		for s, e in skip_ranges:
			if start == s and end == e:
				return True
		return False
	out_parts: list[str] = []
	last = 0
	for m in _SIG_TOKEN_RE.finditer(signature):
		start, end = m.span()
		if start > last:
			out_parts.append(html.escape(signature[last:start]))
		token = m.group(1)
		if in_skip(start, end):
			out_parts.append(html.escape(token))
		elif token in _NON_SYMBOL_TOKENS:
			out_parts.append(f'<span class="kw">{html.escape(token)}</span>')
		elif link_types:
			entry = resolve_symbol(site, token)
			display = _strip_b3d(token)
			if entry is not None:
				href = relative_link(current_url, entry.url)
				out_parts.append(
					f'<a class="type-ref" href="{href}">{html.escape(display)}</a>'
				)
			else:
				out_parts.append(html.escape(display))
		else:
			out_parts.append(html.escape(_strip_b3d(token)))
		last = end
	if last < len(signature):
		out_parts.append(html.escape(signature[last:]))
	return "".join(out_parts)


def _linkify_type_name(name: str, site: Site, current_url: str) -> str:
	"""Linkify a single type name (used for bases). Strips access keywords."""
	s = re.sub(r"^\s*(public|protected|private|virtual)\s+", "", name).strip()
	if not s:
		return ""
	# Drop template args for lookup but preserve them for display.
	lookup = re.sub(r"<.*?>", "", s).strip()
	entry = resolve_symbol(site, lookup)
	display = _strip_b3d(s)
	if entry is None:
		return html.escape(display)
	href = relative_link(current_url, entry.url)
	return f'<a class="type-ref" href="{href}">{html.escape(display)}</a>'


def _render_qname_link(
	qname: str,
	url: str,
	current_url: str,
) -> str:
	"""Render a qualified name where the leading namespace (minus the implicit
	``b3d::``) is plain text and only the final simple name is linked. Matches
	user expectation for group listings where the namespace provides context
	without cluttering the click target."""
	display = _strip_b3d(qname)
	href = relative_link(current_url, url)
	if "::" in display:
		idx = display.rfind("::")
		ns = display[: idx + 2]
		simple = display[idx + 2 :]
		return (
			f'<span class="qname-ns">{html.escape(ns)}</span>'
			f'<a href="{href}">{html.escape(simple)}</a>'
		)
	return f'<a href="{href}">{html.escape(display)}</a>'


def _make_env(template_dir: Path) -> Environment:
	env = Environment(
		loader=FileSystemLoader(str(template_dir)),
		autoescape=select_autoescape(["html"]),
		keep_trailing_newline=True,
	)
	return env


def _resolve_b3d_refs(html_out: str, site: Site, current_url: str) -> str:
	def fn_sub(m):
		sym = m.group(1)
		entry = resolve_symbol(site, sym)
		if entry is None:
			return f'<code class="api-ref unresolved">{sym}</code>'
		href = relative_link(current_url, entry.url)
		return f'<a class="api-ref" href="{href}"><code>{sym}</code></a>'
	return _B3D_REF_RE.sub(fn_sub, html_out)


def _render_inline_markdown(text: str, site: Site, current_url: str) -> str:
	"""Render a Markdown block (multi-paragraph description) to HTML and
	rewrite @b3d:: references to links."""
	if not text:
		return ""
	md = MarkdownIt("commonmark", {"html": False}).enable("table")
	html_out = md.render(text)
	return _resolve_b3d_refs(html_out, site, current_url)


def _render_doc_inline(text: str, site: Site, current_url: str) -> str:
	"""Render a short Markdown fragment (brief, param description, return)
	without wrapping it in a <p>, so it can sit inside existing prose."""
	if not text:
		return ""
	md = MarkdownIt("commonmark", {"html": False})
	html_out = md.renderInline(text)
	return _resolve_b3d_refs(html_out, site, current_url)


_VIS_ORDER = ("public", "internal", "protected", "private")


def _empty_vis_buckets() -> dict:
	return {vis: {"methods": [], "fields": []} for vis in _VIS_ORDER}


def _min_visibility(member_vis: str, inherit_access: str) -> str:
	"""Effective visibility of an inherited member: the most restrictive of
	the member's own visibility and the inheritance access specifier."""
	order = {"public": 0, "protected": 1, "private": 2, "internal": 3}
	if inherit_access == "private":
		return "private"
	if inherit_access == "protected":
		if member_vis == "public":
			return "protected"
		return member_vis
	return member_vis


def _should_flatten_base(base_str: str, base_cls: Class) -> bool:
	"""A base class is "transparent" (its members are folded into the derived
	class as if declared there) when the inheritance targets a template
	instantiation or when the base is a plain struct. Everything else is a
	normal base class whose members stay on the base class's own page."""
	if "<" in base_str:
		return True
	if base_cls.template_params:
		return True
	if base_cls.kind == "struct":
		return True
	return False


def _build_class_sections(cls: Class, site: Site) -> dict:
	"""Produce the nested data structure consumed by the class template.

	Return shape:
		{
			'own': {vis: {'methods': [Member], 'fields': [Member]}},
			'inherited': {vis: []},   # kept as empty placeholder
		}

	Regular (non-templated, non-struct) base classes contribute nothing —
	users navigate to the base class page via the "Inherits:" header if they
	want to see its members. Templated and struct bases are merged into the
	derived class's own members as if declared locally.
	"""
	own = _empty_vis_buckets()
	covered_method_names: set[str] = set()
	for m in cls.members:
		own[m.visibility]["fields" if m.kind == "field" else "methods"].append(m)
		if m.kind != "field":
			covered_method_names.add(m.name)

	visited: set[str] = {cls.qualified_name}

	def merge(base_cls: Class, inherit_access: str) -> None:
		if base_cls.qualified_name in visited:
			return
		visited.add(base_cls.qualified_name)
		for m in base_cls.members:
			# Private members of a base are never accessible.
			if m.visibility == "private":
				continue
			effective = _min_visibility(m.visibility, inherit_access)
			if m.kind == "field":
				own[effective]["fields"].append(m)
			else:
				if m.name in covered_method_names:
					continue
				covered_method_names.add(m.name)
				own[effective]["methods"].append(m)
		# Recurse through transitive templated/struct bases only — a regular
		# class base terminates the flattening chain.
		for next_base_str in base_cls.bases:
			next_cls = resolve_base_class(next_base_str, site)
			if next_cls is None:
				continue
			if not _should_flatten_base(next_base_str, next_cls):
				continue
			next_access = inherit_access
			m_acc = re.match(r"^\s*(public|protected|private)\s+", next_base_str)
			if m_acc:
				if m_acc.group(1) == "private":
					next_access = "private"
				elif m_acc.group(1) == "protected" and inherit_access == "public":
					next_access = "protected"
			merge(next_cls, next_access)

	for base_str in cls.bases:
		base_cls = resolve_base_class(base_str, site)
		if base_cls is None:
			continue
		if not _should_flatten_base(base_str, base_cls):
			continue
		m_acc = re.match(r"^\s*(public|protected|private)\s+", base_str)
		access = m_acc.group(1) if m_acc else "public"
		merge(base_cls, access)

	return {"own": own, "inherited": {vis: [] for vis in _VIS_ORDER}}


def _section_has_content(own_bucket: dict, inherited_list: list) -> bool:
	if own_bucket["methods"] or own_bucket["fields"]:
		return True
	for entry in inherited_list:
		if entry["methods"] or entry["fields"]:
			return True
	return False


def render_site(
	site: Site,
	template_dir: Path,
	output_dir: Path,
) -> None:
	env = _make_env(template_dir)

	def url_for_group(name: str) -> str:
		return f"api/groups/{_safe(name)}.html"

	env.globals["url_for_group"] = url_for_group
	env.globals["relative_link"] = relative_link
	env.globals["render_doc_md"] = lambda text, current: _render_inline_markdown(text, site, current)
	env.globals["render_doc_inline"] = lambda text, current: _render_doc_inline(text, site, current)
	env.globals["render_signature"] = lambda sig, current: _linkify_signature(sig, site, current)
	env.globals["render_signature_plain"] = lambda sig, current: _linkify_signature(
		_rewrite_to_trailing_return(sig), site, current, link_types=False
	)
	env.globals["render_type"] = lambda name, current: _linkify_type_name(name, site, current)
	env.globals["render_qname_link"] = lambda qname, url, current: _render_qname_link(qname, url, current)
	env.globals["strip_b3d"] = _strip_b3d

	# Front page
	_write(
		output_dir / "index.html",
		env.get_template("index.html").render(
			current_url="index.html",
			site=site,
			page_title="Banshee Engine Documentation",
		),
	)

	# API index
	_write(
		output_dir / "api" / "index.html",
		env.get_template("api_index.html").render(
			current_url="api/index.html",
			site=site,
			page_title="API Reference",
			root_groups=[site.groups[n] for n in site.root_group_order if n in site.groups],
		),
	)

	# Group pages
	group_tpl = env.get_template("api_group.html")
	for g in site.groups.values():
		url = url_for_group(g.name)
		html_out = group_tpl.render(
			current_url=url,
			site=site,
			page_title=g.title,
			group=g,
		)
		_write(output_dir / url, html_out)

	# Class pages
	class_tpl = env.get_template("api_class.html")
	for cls in site.classes.values():
		data = _build_class_sections(cls, site)
		html_out = class_tpl.render(
			current_url=cls.url,
			site=site,
			page_title=f"{cls.kind} {cls.qualified_name}",
			cls=cls,
			own=data["own"],
			inherited=data["inherited"],
			section_has_content=_section_has_content,
			vis_order=_VIS_ORDER,
		)
		_write(output_dir / cls.url, html_out)

	# Enum pages
	enum_tpl = env.get_template("api_enum.html")
	for enum in site.enums.values():
		html_out = enum_tpl.render(
			current_url=enum.url,
			site=site,
			page_title=f"enum {enum.qualified_name}",
			enum=enum,
		)
		_write(output_dir / enum.url, html_out)

	# Manual pages
	manual_tpl = env.get_template("manual_page.html")
	for manual in site.manuals.values():
		url = f"manuals/{manual.slug}.html"
		html_out = manual_tpl.render(
			current_url=url,
			site=site,
			page_title=manual.title,
			manual=manual,
		)
		_write(output_dir / url, html_out)

	# Manual index
	_write(
		output_dir / "manuals" / "index.html",
		env.get_template("manual_index.html").render(
			current_url="manuals/index.html",
			site=site,
			page_title="Manuals",
		),
	)


def _safe(name: str) -> str:
	from .util import safe_filename
	return safe_filename(name)


def _write(path: Path, content: str) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(content, encoding="utf-8")
