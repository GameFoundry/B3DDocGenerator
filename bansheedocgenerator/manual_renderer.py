"""Manual renderer: converts Markdown manuals to HTML with @b3d:: link rewriting.

Uses markdown-it-py. Runs two AST-level rewrites:

	1. Text token scan for `@b3d::Foo(::Bar)*` — replaced with inline link
	   tokens pointing at the API pages via the symbol index.
	2. Relative .md link rewriting — converts `../foo/bar.md#anchor` to the
	   generated .html path, preserving fragments.

Images under `../../Images/` are rewritten to `images/` relative to the
manuals tree in the generated site.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath

import html as _html

from markdown_it import MarkdownIt
from markdown_it.token import Token
from mdit_py_plugins.anchors import anchors_plugin

from .ir_builder import resolve_symbol
from .model import Manual, Site
from .util import relative_link, warn


_B3D_REF_RE = re.compile(r"@b3d::([A-Za-z_][\w]*(?:::[A-Za-z_~][\w]*)*)")

# Map a few casual language tags to canonical highlight.js names.
_LANG_ALIASES = {
	"c++": "cpp",
	"cxx": "cpp",
	"hpp": "cpp",
	"h": "cpp",
	"c#": "csharp",
	"cs": "csharp",
	"py": "python",
	"js": "javascript",
	"ts": "typescript",
	"sh": "bash",
	"shell": "bash",
}


def _make_md() -> MarkdownIt:
	md = MarkdownIt("commonmark", {"html": True}).enable("table")
	md = md.use(anchors_plugin, max_level=3, permalink=False)
	return md


_LANG_INFO_RE = re.compile(r"\{?\s*\.?([\w+#-]+)")


def _render_code_fence(code: str, info: str | None) -> str:
	"""Emit a plain language-tagged code block for client-side highlighting.

	Code is HTML-escaped here; highlight.js adds span classes in-place without
	re-escaping. If the fence carries no language, assume C++ — the Banshee
	codebase uses it everywhere. Accepts both plain `cpp` and the Daux-style
	attribute form `{.cpp}` used in the existing manuals."""
	raw = (info or "").strip().lower()
	lang = "cpp"
	if raw:
		m = _LANG_INFO_RE.match(raw)
		if m:
			lang = m.group(1)
	lang = _LANG_ALIASES.get(lang, lang)
	escaped = _html.escape(code)
	return f'<pre class="code"><code class="language-{lang}">{escaped}</code></pre>'


def _rewrite_b3d_refs_in_text(text_token: Token, site: Site, current_page_url: str) -> list[Token]:
	"""If `text_token.content` contains @b3d:: refs, split into text+link tokens."""
	content = text_token.content
	if "@b3d::" not in content:
		return [text_token]
	parts: list[Token] = []
	last = 0
	for m in _B3D_REF_RE.finditer(content):
		if m.start() > last:
			t = Token("text", "", 0)
			t.content = content[last : m.start()]
			parts.append(t)
		sym = m.group(1)
		entry = resolve_symbol(site, sym)
		if entry is None:
			# Leave as code span so it's visually distinct but still reads.
			t = Token("code_inline", "code", 0)
			t.content = sym
			parts.append(t)
			warn(f"unresolved @b3d::{sym}")
		else:
			link_open = Token("link_open", "a", 1)
			href = relative_link(current_page_url, entry.url)
			link_open.attrs = {"href": href, "class": "api-ref"}
			inner = Token("code_inline", "code", 0)
			inner.content = sym
			link_close = Token("link_close", "a", -1)
			parts.extend([link_open, inner, link_close])
		last = m.end()
	if last < len(content):
		t = Token("text", "", 0)
		t.content = content[last:]
		parts.append(t)
	return parts


def _rewrite_tokens(tokens: list[Token], site: Site, current_page_url: str) -> list[Token]:
	out: list[Token] = []
	for tok in tokens:
		if tok.type == "inline" and tok.children:
			new_children: list[Token] = []
			for child in tok.children:
				if child.type == "text":
					new_children.extend(
						_rewrite_b3d_refs_in_text(child, site, current_page_url)
					)
				elif child.type == "link_open":
					href = child.attrGet("href") or ""
					new_href = _rewrite_link(href, current_page_url)
					if new_href != href:
						child.attrSet("href", new_href)
					new_children.append(child)
				else:
					new_children.append(child)
			tok.children = new_children
			out.append(tok)
		else:
			out.append(tok)
	return out


def _rewrite_link(href: str, current_page_url: str) -> str:
	if not href or href.startswith(("http://", "https://", "mailto:", "#")):
		return href
	fragment = ""
	if "#" in href:
		href, fragment = href.split("#", 1)
		fragment = "#" + fragment
	if href.endswith(".md"):
		href = href[:-3] + ".html"
	# images: '../../Images/foo.png' → 'images/foo.png' relative to manuals/
	if "Images/" in href:
		# Extract the basename after Images/
		idx = href.rfind("Images/")
		basename = href[idx + len("Images/") :]
		target = f"manuals/images/{basename}"
		return relative_link(current_page_url, target) + fragment
	# Relative .md/.html link: resolve against the current manual's source dir.
	cur_parts = current_page_url.split("/")
	base_parts = cur_parts[:-1]
	parts = href.split("/")
	for p in parts:
		if p == "..":
			if base_parts:
				base_parts.pop()
		elif p == ".":
			continue
		else:
			base_parts.append(p)
	resolved = "/".join(base_parts)
	return relative_link(current_page_url, resolved) + fragment


def render_manual(manual: Manual, site: Site) -> None:
	"""Render `manual` to HTML (populates manual.html)."""
	try:
		with open(manual.source_path, encoding="utf-8") as f:
			text = f.read()
	except OSError as e:
		warn(f"could not read {manual.source_path}: {e}")
		manual.html = ""
		return
	# Strip YAML frontmatter
	if text.startswith("---\n"):
		end = text.find("\n---\n", 4)
		if end != -1:
			text = text[end + 5 :]

	md = _make_md()
	# Emit language-tagged code blocks; the browser-side highlight.js loader
	# in base.html converts them in place.
	def fence_renderer(self, tokens, idx, options, env):  # noqa: ARG001, N802
		token = tokens[idx]
		return _render_code_fence(token.content, token.info)

	md.add_render_rule("fence", fence_renderer)

	current_page_url = f"manuals/{manual.slug}.html"
	env: dict = {}
	tokens = md.parse(text, env)
	tokens = _rewrite_tokens(tokens, site, current_page_url)
	# Collect headings
	manual.headings = []
	for i, tok in enumerate(tokens):
		if tok.type == "heading_open":
			level = int(tok.tag[1])
			anchor = tok.attrGet("id") or ""
			# Next token should be the inline with the heading text
			if i + 1 < len(tokens) and tokens[i + 1].type == "inline":
				text_val = "".join(
					c.content for c in (tokens[i + 1].children or []) if c.type == "text"
				).strip()
			else:
				text_val = ""
			manual.headings.append((level, text_val, anchor))
	manual.html = md.renderer.render(tokens, md.options, env)


def render_manuals(site: Site) -> None:
	for manual in site.manuals.values():
		render_manual(manual, site)
