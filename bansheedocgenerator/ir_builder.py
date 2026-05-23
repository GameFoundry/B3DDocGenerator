"""IR builder: normalizes raw parser output into the Site data model.

Responsibilities:
	- Group RawDecls by the class they belong to (or leave them as free
	  functions/enums/classes at namespace scope).
	- Assign URLs and anchors for every documented entity.
	- Propagate is_internal (from groups, from @name Internal, from name).
	- Resolve @copydoc.
	- Build a symbol index mapping both qualified and simple names to URLs.
"""

from __future__ import annotations

import re
from collections import defaultdict

from .config import INTERNAL_MARKER, PRIMARY_NAMESPACE
from .group_resolver import internal_partner_name
from .model import (
	Class,
	DocBlock,
	Enum,
	EnumValue,
	FreeFunction,
	Group,
	Member,
	RawDecl,
	Site,
	SourceLoc,
	SymbolEntry,
)
from .util import safe_anchor, safe_filename, warn


UNCATEGORIZED_GROUP_NAME = "Uncategorized"
UNCATEGORIZED_GROUP_TITLE = "Uncategorized"
UNCATEGORIZED_GROUP_DESCRIPTION = (
	"Symbols that have no ``@defgroup`` or ``@addtogroup`` scope assigned."
)


def build_ir(
	raw_decls: list[RawDecl],
	groups: dict[str, Group],
	site: Site,
) -> None:
	site.groups = groups

	# Split raws by kind.
	class_raws: dict[str, RawDecl] = {}
	member_raws: list[RawDecl] = []
	enum_raws: list[RawDecl] = []
	function_raws: list[RawDecl] = []

	for d in raw_decls:
		if d.kind in ("class", "struct"):
			# If we've seen this qualified name before, prefer the one with
			# a doc block; otherwise keep the first.
			if d.qualified_name in class_raws:
				existing = class_raws[d.qualified_name]
				if not existing.doc.brief and not existing.doc.description and (
					d.doc.brief or d.doc.description
				):
					class_raws[d.qualified_name] = d
			else:
				class_raws[d.qualified_name] = d
		elif d.kind in ("method", "field"):
			member_raws.append(d)
		elif d.kind == "enum":
			enum_raws.append(d)
		elif d.kind == "function":
			function_raws.append(d)

	# Build Class entries
	for qname, d in class_raws.items():
		url = f"api/classes/{safe_filename(qname)}.html"
		cls = Class(
			kind=d.kind,
			name=d.name,
			qualified_name=qname,
			template_params=d.template_params,
			bases=d.bases,
			namespace=d.namespace,
			group_names=list(d.group_stack),
			visibility=d.visibility or "public",
			doc=d.doc or DocBlock(),
			location=d.location,
			url=url,
		)
		cls.is_internal = (
			_is_internal(cls.group_names, groups)
			or INTERNAL_MARKER in qname.lower()
			or d.is_internal_name_block
		)
		site.classes[qname] = cls

	# Attach members
	members_by_parent: dict[str, list[RawDecl]] = defaultdict(list)
	for m in member_raws:
		if m.parent_class_qname:
			members_by_parent[m.parent_class_qname].append(m)

	for parent_qname, mlist in members_by_parent.items():
		cls = site.classes.get(parent_qname)
		if cls is None:
			continue
		# Assign overload indices by name.
		name_counts: dict[str, int] = defaultdict(int)
		for raw in mlist:
			idx = name_counts[raw.name]
			name_counts[raw.name] += 1
			visibility = raw.visibility
			if raw.is_internal_name_block:
				visibility = "internal"
			anchor = f"mem-{visibility}-{safe_anchor(raw.name)}"
			if idx > 0:
				anchor += f"-{idx}"
			member = Member(
				kind=raw.kind,
				name=raw.name,
				qualified_name=raw.qualified_name,
				signature=raw.signature,
				anchor=anchor,
				visibility=visibility,
				is_internal_name_block=raw.is_internal_name_block,
				is_static=raw.is_static,
				is_virtual=raw.is_virtual,
				is_const=raw.is_const,
				is_constructor=raw.is_constructor,
				is_operator=raw.is_operator,
				template_params=raw.template_params,
				return_type=raw.return_type,
				param_list=raw.param_list,
				default_value=raw.default_value,
				doc=raw.doc or DocBlock(),
				location=raw.location,
				overload_index=idx,
			)
			cls.members.append(member)

	# Build Enum entries
	for d in enum_raws:
		url = f"api/enums/{safe_filename(d.qualified_name)}.html"
		enum = Enum(
			name=d.name,
			qualified_name=d.qualified_name,
			underlying=d.enum_underlying,
			is_class_enum=d.is_enum_class,
			values=list(d.enum_values),
			namespace=d.namespace,
			group_names=list(d.group_stack),
			visibility=d.visibility or "public",
			doc=d.doc or DocBlock(),
			location=d.location,
			url=url,
		)
		enum.is_internal = (
			_is_internal(enum.group_names, groups)
			or INTERNAL_MARKER in d.qualified_name.lower()
			or d.is_internal_name_block
		)
		site.enums[d.qualified_name] = enum

	# Build FreeFunction entries (grouped on their group's page)
	name_counts: dict[str, int] = defaultdict(int)
	for d in function_raws:
		idx = name_counts[d.qualified_name]
		name_counts[d.qualified_name] += 1
		group_name = d.group_stack[-1] if d.group_stack else UNCATEGORIZED_GROUP_NAME
		anchor = f"fn-{safe_anchor(d.name)}"
		if idx > 0:
			anchor += f"-{idx}"
		url = f"api/groups/{safe_filename(group_name)}.html#{anchor}"
		fn = FreeFunction(
			name=d.name,
			qualified_name=d.qualified_name,
			signature=d.signature,
			return_type=d.return_type,
			param_list=d.param_list,
			template_params=d.template_params,
			namespace=d.namespace,
			group_names=list(d.group_stack),
			doc=d.doc or DocBlock(),
			location=d.location,
			url=url,
			anchor=anchor,
			overload_index=idx,
		)
		fn.is_internal = _is_internal(fn.group_names, groups) or d.is_internal_name_block
		# Differentiate overloads in the qualified_name key
		key = d.qualified_name if idx == 0 else f"{d.qualified_name}#{idx}"
		site.functions[key] = fn

	# Populate group -> entity lists and ensure groups referenced exist.
	for qname, cls in site.classes.items():
		for gname in cls.group_names:
			g = site.groups.setdefault(gname, Group(name=gname, title=gname))
			if INTERNAL_MARKER in gname.lower():
				g.is_internal = True
			if qname not in g.classes:
				g.classes.append(qname)
	for qname, enum in site.enums.items():
		for gname in enum.group_names:
			g = site.groups.setdefault(gname, Group(name=gname, title=gname))
			if INTERNAL_MARKER in gname.lower():
				g.is_internal = True
			if qname not in g.enums:
				g.enums.append(qname)
	for key, fn in site.functions.items():
		for gname in fn.group_names:
			g = site.groups.setdefault(gname, Group(name=gname, title=gname))
			if INTERNAL_MARKER in gname.lower():
				g.is_internal = True
			if key not in g.functions:
				g.functions.append(key)

	# Nested classes/enums whose C++ access level is not public are treated
	# as internal in the group listings (they still have their own pages —
	# this only affects which section they show up in on the enclosing
	# group page, and whether search surfaces them with internals off).
	_mark_nested_nonpublic_internal(site)

	# Any decl that reached the IR without a @defgroup / @addtogroup scope
	# would otherwise be invisible from the nav tree. Park those under a
	# synthetic "Uncategorized" root group so every symbol is reachable.
	_assign_uncategorized(site)

	# Ensure every referenced group has a URL (via the render phase; here we
	# just build root_group_order from groups that have no parent).
	if not site.root_group_order:
		site.root_group_order = [
			g.name for g in site.groups.values() if not g.parent
		]
		site.root_group_order.sort(key=lambda n: (site.groups[n].order, n))

	# Resolve @copydoc (max 3 hops)
	_resolve_copydoc(site)

	# Fold "Category-Internal" groups into their "Category" partners so the
	# public and internal entries for the same area render on a single page,
	# with the internal ones toggle-hidden via their is_internal flag.
	_merge_internal_groups(site)

	# Build symbol index — required by override-doc resolution below
	# (it needs to resolve base class names against the index).
	_build_symbol_index(site)

	# Resolve override-inherited documentation (override methods without a
	# doc block pull from the corresponding base class method, recursively).
	_resolve_override_docs(site)


def _is_internal(group_names: list[str], groups: dict[str, Group]) -> bool:
	for name in group_names:
		if INTERNAL_MARKER in name.lower():
			return True
		g = groups.get(name)
		if g and g.is_internal:
			return True
	return False


def _resolve_copydoc(site: Site) -> None:
	def get_doc_by_qname(qname: str) -> DocBlock | None:
		# Class?
		cls = site.classes.get(qname)
		if cls:
			return cls.doc
		# Enum?
		enum = site.enums.get(qname)
		if enum:
			return enum.doc
		# Member?
		if "::" in qname:
			parent = qname.rsplit("::", 1)[0]
			name = qname.rsplit("::", 1)[1]
			c = site.classes.get(parent)
			if c:
				for m in c.members:
					if m.name == name:
						return m.doc
		# Free function?
		fn = site.functions.get(qname)
		if fn:
			return fn.doc
		return None

	def resolve(doc: DocBlock, hops: int) -> None:
		if hops > 3 or not doc.copydoc_target:
			return
		target_name = doc.copydoc_target.split("(")[0]
		target_doc = get_doc_by_qname(target_name) or get_doc_by_qname(f"b3d::{target_name}")
		if target_doc and target_doc is not doc:
			resolve(target_doc, hops + 1)
			if not doc.brief:
				doc.brief = target_doc.brief
			if not doc.description:
				doc.description = target_doc.description
			if not doc.params:
				doc.params = list(target_doc.params)
			if not doc.returns:
				doc.returns = target_doc.returns

	for cls in site.classes.values():
		resolve(cls.doc, 0)
		for m in cls.members:
			resolve(m.doc, 0)
	for enum in site.enums.values():
		resolve(enum.doc, 0)
	for fn in site.functions.values():
		resolve(fn.doc, 0)


def _mark_nested_nonpublic_internal(site: Site) -> None:
	"""Any class/struct/enum nested inside another class carries a C++ access
	specifier (public/protected/private). Non-public nested types should not
	clutter the public group listings, so we fold them into the same
	``is_internal`` bucket as ``@name Internal`` blocks and internal groups.
	Sub-namespaces (``b3d::detail``, ``b3d::render``, …) are NOT classes and
	must never trigger this — the guard is ``parent resolves to a class``."""
	for cls in site.classes.values():
		if cls.is_internal:
			continue
		if "::" not in cls.qualified_name:
			continue
		parent_qname = cls.qualified_name.rsplit("::", 1)[0]
		if parent_qname not in site.classes:
			continue
		if cls.visibility and cls.visibility != "public":
			cls.is_internal = True

	for enum in site.enums.values():
		if enum.is_internal:
			continue
		if "::" not in enum.qualified_name:
			continue
		parent_qname = enum.qualified_name.rsplit("::", 1)[0]
		if parent_qname not in site.classes:
			continue
		if enum.visibility and enum.visibility != "public":
			enum.is_internal = True


def _assign_uncategorized(site: Site) -> None:
	"""Collect every class/enum/free-function with an empty group_names and
	park them in a synthetic ``Uncategorized`` root group. The group is only
	materialized if at least one orphan exists; otherwise nothing happens.
	Symbols outside the ``b3d`` namespace (third-party helpers, ``std``, etc.)
	are ignored — we only document engine-owned code."""
	def _in_b3d(ns: str) -> bool:
		return ns == PRIMARY_NAMESPACE or ns.startswith(PRIMARY_NAMESPACE + "::")

	orphan_classes = [q for q, c in site.classes.items() if not c.group_names and _in_b3d(c.namespace)]
	orphan_enums = [q for q, e in site.enums.items() if not e.group_names and _in_b3d(e.namespace)]
	orphan_functions = [k for k, f in site.functions.items() if not f.group_names and _in_b3d(f.namespace)]
	if not orphan_classes and not orphan_enums and not orphan_functions:
		return

	g = site.groups.get(UNCATEGORIZED_GROUP_NAME)
	if g is None:
		g = Group(
			name=UNCATEGORIZED_GROUP_NAME,
			title=UNCATEGORIZED_GROUP_TITLE,
			description=UNCATEGORIZED_GROUP_DESCRIPTION,
		)
		site.groups[UNCATEGORIZED_GROUP_NAME] = g
	if UNCATEGORIZED_GROUP_NAME not in site.root_group_order:
		site.root_group_order.append(UNCATEGORIZED_GROUP_NAME)

	for qname in orphan_classes:
		cls = site.classes[qname]
		cls.group_names.append(UNCATEGORIZED_GROUP_NAME)
		if qname not in g.classes:
			g.classes.append(qname)
	for qname in orphan_enums:
		enum = site.enums[qname]
		enum.group_names.append(UNCATEGORIZED_GROUP_NAME)
		if qname not in g.enums:
			g.enums.append(qname)
	for key in orphan_functions:
		fn = site.functions[key]
		fn.group_names.append(UNCATEGORIZED_GROUP_NAME)
		if key not in g.functions:
			g.functions.append(key)


def _merge_internal_groups(site: Site) -> None:
	"""Fold each internal group with a public partner into that partner.

	Classes, enums and free functions are appended to the partner's lists
	(de-duped). Free-function URLs are rewritten so anchor links on the
	partner page resolve correctly. Entity ``group_names`` entries are
	substituted so breadcrumbs point at the partner group. The merged internal
	group is then removed from its parent's children list, from
	``site.root_group_order``, and finally from ``site.groups`` so no standalone
	page is emitted for it. Internal groups without a partner (e.g.
	``Renderer-Internal``) are left untouched."""
	pairs: list[tuple[str, str]] = []
	for name, g in list(site.groups.items()):
		if not g.is_internal:
			continue
		partner_name = internal_partner_name(name, site.groups)
		if partner_name is None:
			continue
		pairs.append((name, partner_name))

	def _rename_in(group_names: list[str], old: str, new: str) -> None:
		for i, n in enumerate(group_names):
			if n == old:
				group_names[i] = new

	for internal_name, partner_name in pairs:
		internal = site.groups[internal_name]
		partner = site.groups[partner_name]

		for qname in internal.classes:
			if qname not in partner.classes:
				partner.classes.append(qname)
			cls = site.classes.get(qname)
			if cls is not None:
				_rename_in(cls.group_names, internal_name, partner_name)
		for qname in internal.enums:
			if qname not in partner.enums:
				partner.enums.append(qname)
			enum = site.enums.get(qname)
			if enum is not None:
				_rename_in(enum.group_names, internal_name, partner_name)
		for fkey in internal.functions:
			if fkey not in partner.functions:
				partner.functions.append(fkey)
			fn = site.functions.get(fkey)
			if fn is None:
				continue
			_rename_in(fn.group_names, internal_name, partner_name)
			fn.url = f"api/groups/{safe_filename(partner_name)}.html#{fn.anchor}"

		if internal.parent and internal.parent in site.groups:
			parent = site.groups[internal.parent]
			if internal_name in parent.children:
				parent.children.remove(internal_name)
		if internal_name in site.root_group_order:
			site.root_group_order.remove(internal_name)
		del site.groups[internal_name]


_BASE_ACCESS_RE = re.compile(r"^\s*(public|protected|private|virtual)\s+")
_BASE_TEMPLATE_RE = re.compile(r"<.*?>")


def resolve_base_class(base_str: str, site: Site) -> "Class | None":
	"""Resolve an inheritance list entry (e.g. 'public TMaterial<false>') to a
	Class in the site, or None if it cannot be found."""
	s = _BASE_ACCESS_RE.sub("", base_str).strip()
	# Also strip leading 'virtual' when it follows 'public' etc.
	s = _BASE_ACCESS_RE.sub("", s).strip()
	s = _BASE_TEMPLATE_RE.sub("", s).strip()
	if not s:
		return None
	entry = resolve_symbol(site, s)
	if entry is None:
		return None
	return site.classes.get(entry.qualified_name)


def _resolve_override_docs(site: Site) -> None:
	"""For each override method with no local docs, copy the doc block from
	the first matching method in the base-class chain."""

	def lookup(cls: "Class", name: str, visited: set) -> DocBlock | None:
		for base_str in cls.bases:
			base_cls = resolve_base_class(base_str, site)
			if base_cls is None or base_cls.qualified_name in visited:
				continue
			visited.add(base_cls.qualified_name)
			for m in base_cls.members:
				if m.kind == "field" or m.name != name:
					continue
				if m.doc.brief or m.doc.description or m.doc.params or m.doc.returns:
					return m.doc
				deeper = lookup(base_cls, name, visited)
				if deeper is not None:
					return deeper
			deeper = lookup(base_cls, name, visited)
			if deeper is not None:
				return deeper
		return None

	for cls in site.classes.values():
		for m in cls.members:
			if m.kind == "field":
				continue
			if m.doc.brief or m.doc.description:
				continue
			if " override" not in m.signature:
				continue
			base_doc = lookup(cls, m.name, set())
			if base_doc is None:
				continue
			if not m.doc.brief:
				m.doc.brief = base_doc.brief
			if not m.doc.description:
				m.doc.description = base_doc.description
			if not m.doc.params:
				m.doc.params = list(base_doc.params)
			if not m.doc.returns:
				m.doc.returns = base_doc.returns


def _build_symbol_index(site: Site) -> None:
	def add(key: str, entry: SymbolEntry) -> None:
		site.symbol_index.setdefault(key, []).append(entry)

	for cls in site.classes.values():
		e = SymbolEntry(
			qualified_name=cls.qualified_name,
			kind=cls.kind,
			url=cls.url,
			is_internal=cls.is_internal,
		)
		add(cls.qualified_name, e)
		add(cls.name, e)
		for m in cls.members:
			if m.overload_index > 0:
				# Only register the first overload.
				continue
			me = SymbolEntry(
				qualified_name=m.qualified_name,
				kind=m.kind,
				url=f"{cls.url}#{m.anchor}",
				is_internal=cls.is_internal or m.visibility in ("internal", "private", "protected"),
			)
			add(m.qualified_name, me)
			add(f"{cls.name}::{m.name}", me)
	for enum in site.enums.values():
		e = SymbolEntry(
			qualified_name=enum.qualified_name,
			kind="enum",
			url=enum.url,
			is_internal=enum.is_internal,
		)
		add(enum.qualified_name, e)
		add(enum.name, e)
		for v in enum.values:
			ve = SymbolEntry(
				qualified_name=f"{enum.qualified_name}::{v.name}",
				kind="enum_value",
				url=f"{enum.url}#val-{safe_anchor(v.name)}",
				is_internal=enum.is_internal,
			)
			add(ve.qualified_name, ve)
			add(f"{enum.name}::{v.name}", ve)
	for fn in site.functions.values():
		e = SymbolEntry(
			qualified_name=fn.qualified_name,
			kind="function",
			url=fn.url,
			is_internal=fn.is_internal,
		)
		add(fn.qualified_name, e)
		add(fn.name, e)


def resolve_symbol(site: Site, name: str) -> SymbolEntry | None:
	"""Look up a qualified or simple name in the symbol index.

	Preference order:
		1. Exact hit on the qualified name.
		2. Exact hit under `b3d::...` prefix.
		3. First entry returned by the bucket (arbitrary).
	"""
	bucket = site.symbol_index.get(name)
	if bucket:
		# Prefer a qualified-name match when multiple entries share a simple name.
		for e in bucket:
			if e.qualified_name == name:
				return e
		return bucket[0]
	# Try stripping / adding b3d:: prefix
	if name.startswith("b3d::"):
		return resolve_symbol(site, name[len("b3d::") :])
	qname = f"b3d::{name}"
	bucket = site.symbol_index.get(qname)
	if bucket:
		return bucket[0]
	return None
