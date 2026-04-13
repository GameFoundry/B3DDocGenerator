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

from .config import INTERNAL_MARKER
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
			doc=d.doc or DocBlock(),
			location=d.location,
			url=url,
		)
		cls.is_internal = _is_internal(cls.group_names, groups) or INTERNAL_MARKER in qname.lower()
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
			doc=d.doc or DocBlock(),
			location=d.location,
			url=url,
		)
		enum.is_internal = (
			_is_internal(enum.group_names, groups)
			or INTERNAL_MARKER in d.qualified_name.lower()
		)
		site.enums[d.qualified_name] = enum

	# Build FreeFunction entries (grouped on their group's page)
	name_counts: dict[str, int] = defaultdict(int)
	for d in function_raws:
		idx = name_counts[d.qualified_name]
		name_counts[d.qualified_name] += 1
		group_name = d.group_stack[-1] if d.group_stack else "_ungrouped"
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
		fn.is_internal = _is_internal(fn.group_names, groups)
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

	# Ensure every referenced group has a URL (via the render phase; here we
	# just build root_group_order from groups that have no parent).
	if not site.root_group_order:
		site.root_group_order = [
			g.name for g in site.groups.values() if not g.parent
		]
		site.root_group_order.sort(key=lambda n: (site.groups[n].order, n))

	# Resolve @copydoc (max 3 hops)
	_resolve_copydoc(site)

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
