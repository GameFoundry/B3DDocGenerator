"""Manual scanner — discovers Markdown manuals and extracts titles."""

from __future__ import annotations

import re
from pathlib import Path

from .model import Manual, ManualTreeNode
from .util import to_posix


_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_FRONTMATTER_TITLE_RE = re.compile(r"^title:\s*(.*)\s*$", re.MULTILINE)
_H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
_NUMERIC_PREFIX_RE = re.compile(r"^(\d+)_")


def _order_key(rel_parts: tuple[str, ...]) -> tuple:
	"""Build a tuple that sorts numeric-prefixed filenames naturally."""
	key = []
	for part in rel_parts:
		m = _NUMERIC_PREFIX_RE.match(part)
		if m:
			key.append((int(m.group(1)), part.lower()))
		else:
			key.append((10**9, part.lower()))
	return tuple(key)


def _extract_title(text: str, fallback: str) -> str:
	m = _FRONTMATTER_RE.match(text)
	if m:
		mm = _FRONTMATTER_TITLE_RE.search(m.group(1))
		if mm:
			return mm.group(1).strip()
	m = _H1_RE.search(text)
	if m:
		return m.group(1).strip()
	return fallback


def _humanize_dir(name: str) -> str:
	"""Turn `04_Rendering` / `03_Resources` into `Rendering` / `Resources`."""
	s = _NUMERIC_PREFIX_RE.sub("", name)
	s = s.replace("_", " ").strip()
	return s or name


def scan_manuals(root: Path) -> tuple[dict[str, Manual], list[str], list[ManualTreeNode]]:
	"""Walk `root` and return (manuals_by_slug, ordered_slugs, tree_roots)."""
	if not root.exists():
		return {}, [], []
	manuals: dict[str, Manual] = {}
	md_files = sorted(root.rglob("*.md"))
	for path in md_files:
		rel = path.relative_to(root)
		parts = rel.with_suffix("").parts
		slug = to_posix(Path(*parts))
		try:
			text = path.read_text(encoding="utf-8", errors="replace")
		except OSError:
			continue
		# Skip empty index.md at root — it's just a placeholder.
		if slug == "index" and not text.strip():
			continue
		fallback_name = parts[-1]
		fallback = _NUMERIC_PREFIX_RE.sub("", fallback_name).replace("_", " ").strip() or fallback_name
		title = _extract_title(text, fallback)
		manual = Manual(
			slug=slug,
			title=title,
			order_key=_order_key(parts),
			source_path=to_posix(path),
		)
		manuals[slug] = manual

	ordered = sorted(manuals.keys(), key=lambda s: manuals[s].order_key)

	tree_roots = _build_tree(manuals)
	return manuals, ordered, tree_roots


def _build_tree(manuals: dict[str, Manual]) -> list[ManualTreeNode]:
	"""Construct a nested directory-based tree from flat manual slugs.

	Every directory that contains manuals becomes a node. Files under the
	directory become child nodes. Directories sort by the order key of their
	lowest-numbered child so that `04_Rendering` appears after `03_Resources`.
	"""
	# dir_path -> ManualTreeNode (dir nodes, keyed by posix path; "" = root)
	dir_nodes: dict[str, ManualTreeNode] = {"": ManualTreeNode(title="", dir_path="")}

	def ensure_dir(dir_path: str) -> ManualTreeNode:
		if dir_path in dir_nodes:
			return dir_nodes[dir_path]
		parent_path, _, leaf = dir_path.rpartition("/")
		parent = ensure_dir(parent_path)
		node = ManualTreeNode(
			title=_humanize_dir(leaf),
			dir_path=dir_path,
			order_key=_order_key(tuple(dir_path.split("/"))),
		)
		dir_nodes[dir_path] = node
		parent.children.append(node)
		return node

	for slug, manual in manuals.items():
		parts = slug.split("/")
		dir_path = "/".join(parts[:-1])
		parent = ensure_dir(dir_path)
		leaf = ManualTreeNode(
			title=manual.title,
			slug=slug,
			order_key=manual.order_key,
		)
		parent.children.append(leaf)

	def sort_node(node: ManualTreeNode) -> None:
		def key(n: ManualTreeNode) -> tuple:
			if n.order_key:
				return n.order_key
			if n.children:
				# Inherit the order key of the lowest child.
				child_keys = [c.order_key for c in n.children if c.order_key]
				if child_keys:
					return min(child_keys)
			return ((10**9, n.title.lower()),)
		node.children.sort(key=key)
		for c in node.children:
			sort_node(c)

	root = dir_nodes[""]
	sort_node(root)
	return root.children
