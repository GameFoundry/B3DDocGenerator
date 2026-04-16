"""Group hierarchy resolver.

Takes the flat GroupDecl list emitted by the parser and turns it into a
Group tree with parent/child relationships. The canonical taxonomy lives in
Framework/Source/Engine/Core/B3DPrerequisites.h — its @defgroup declarations
set titles, descriptions, and nesting.
"""

from __future__ import annotations

from .config import INTERNAL_MARKER
from .model import Group, GroupDecl


def internal_partner_name(name: str, groups: dict[str, Group]) -> str | None:
	"""Return the public-facing partner group name for an internal category,
	or None if no partner exists. Matches the two affix conventions used in
	B3DPrerequisites.h: ``Foo-Internal`` → ``Foo`` and ``Internal-Foo`` →
	``Foo``. The match must land on a group that exists and is itself not
	marked internal."""
	candidate: str | None = None
	if name.endswith("-Internal") and len(name) > len("-Internal"):
		candidate = name[: -len("-Internal")]
	elif name.startswith("Internal-") and len(name) > len("Internal-"):
		candidate = name[len("Internal-") :]
	if candidate is None:
		return None
	partner = groups.get(candidate)
	if partner is None or partner.is_internal:
		return None
	return candidate


def resolve_groups(group_decls: list[GroupDecl]) -> tuple[dict[str, Group], list[str]]:
	"""Return (groups_by_name, root_group_order)."""
	groups: dict[str, Group] = {}
	root_order: list[str] = []
	has_defgroup: set[str] = set()

	for gd in group_decls:
		if gd.name not in groups:
			groups[gd.name] = Group(
				name=gd.name,
				title=gd.name,
				defined_in=gd.location,
			)
		g = groups[gd.name]

		if gd.kind == "defgroup":
			has_defgroup.add(gd.name)
			if gd.title:
				g.title = gd.title
			if gd.description:
				g.description = gd.description
			if gd.parent_stack and not g.parent:
				g.parent = gd.parent_stack[-1]

		# Track declaration order for root ordering
		if gd.kind == "defgroup" and not g.parent and g.name not in root_order:
			root_order.append(g.name)

	# Fill in children lists
	for g in groups.values():
		if g.parent and g.parent in groups:
			parent = groups[g.parent]
			if g.name not in parent.children:
				parent.children.append(g.name)

	# Mark internal
	def _mark_internal(name: str, force: bool = False) -> None:
		g = groups.get(name)
		if g is None:
			return
		if force or INTERNAL_MARKER in g.name.lower():
			g.is_internal = True
		for child in g.children:
			_mark_internal(child, force=g.is_internal)

	for name in list(groups.keys()):
		_mark_internal(name)

	# Inherit metadata from the public partner for any internal category
	# that was referenced via @addtogroup but never got its own @defgroup.
	# Without this, such groups render with ``title == name`` and no
	# hierarchy, which looks broken in the nav tree before the IR-level
	# merge collapses them into the partner.
	for name, g in list(groups.items()):
		if not g.is_internal or name in has_defgroup:
			continue
		partner_name = internal_partner_name(name, groups)
		if partner_name is None:
			continue
		partner = groups[partner_name]
		if not g.description and partner.description:
			g.description = partner.description
		if g.parent is None and partner.parent:
			g.parent = partner.parent
			parent_group = groups.get(partner.parent)
			if parent_group is not None and name not in parent_group.children:
				parent_group.children.append(name)

	# Any group that isn't in root_order but has no parent is a root.
	for name, g in groups.items():
		if not g.parent and name not in root_order:
			root_order.append(name)

	# Assign order field
	for i, name in enumerate(root_order):
		groups[name].order = i

	return groups, root_order
