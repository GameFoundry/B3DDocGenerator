"""Build a flat JSON search index consumed by MiniSearch client-side."""

from __future__ import annotations

import json
from pathlib import Path

from .model import Site


def build_search_index(site: Site) -> dict:
	docs = []

	def add(kind: str, name: str, qname: str, url: str, brief: str, is_internal: bool) -> None:
		docs.append(
			{
				"kind": kind,
				"name": name,
				"qname": qname,
				"url": url,
				"brief": brief or "",
				"isInternal": bool(is_internal),
			}
		)

	for cls in site.classes.values():
		add(cls.kind, cls.name, cls.qualified_name, cls.url, cls.doc.brief, cls.is_internal)
		for m in cls.members:
			if m.overload_index > 0:
				continue
			add(
				m.kind,
				m.name,
				m.qualified_name,
				f"{cls.url}#{m.anchor}",
				m.doc.brief,
				cls.is_internal or m.visibility != "public",
			)
	for enum in site.enums.values():
		add("enum", enum.name, enum.qualified_name, enum.url, enum.doc.brief, enum.is_internal)
		for v in enum.values:
			add(
				"enum_value",
				v.name,
				f"{enum.qualified_name}::{v.name}",
				f"{enum.url}#val-{v.name}",
				v.doc.brief,
				enum.is_internal,
			)
	for fn in site.functions.values():
		add("function", fn.name, fn.qualified_name, fn.url, fn.doc.brief, fn.is_internal)
	for manual in site.manuals.values():
		add(
			"manual",
			manual.title,
			manual.title,
			f"manuals/{manual.slug}.html",
			"",
			False,
		)

	return {"docs": docs}


def write_search_index(site: Site, output_dir: Path) -> None:
	data = build_search_index(site)
	out = output_dir / "static" / "search.json"
	out.parent.mkdir(parents=True, exist_ok=True)
	out.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
