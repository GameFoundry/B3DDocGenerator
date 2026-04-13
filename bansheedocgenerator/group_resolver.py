"""Group hierarchy resolver.

Takes the flat GroupDecl list emitted by the parser and turns it into a
Group tree with parent/child relationships. The canonical taxonomy lives in
Framework/Source/Engine/Core/B3DPrerequisites.h — its @defgroup declarations
set titles, descriptions, and nesting.
"""

from __future__ import annotations

from .config import INTERNAL_MARKER
from .model import Group, GroupDecl


def resolve_groups(group_decls: list[GroupDecl]) -> tuple[dict[str, Group], list[str]]:
	"""Return (groups_by_name, root_group_order)."""
	groups: dict[str, Group] = {}
	root_order: list[str] = []

	for gd in group_decls:
		if gd.name not in groups:
			groups[gd.name] = Group(
				name=gd.name,
				title=gd.name,
				defined_in=gd.location,
			)
		g = groups[gd.name]

		if gd.kind == "defgroup":
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

	# Any group that isn't in root_order but has no parent is a root.
	for name, g in groups.items():
		if not g.parent and name not in root_order:
			root_order.append(name)

	# Assign order field
	for i, name in enumerate(root_order):
		groups[name].order = i

	return groups, root_order
