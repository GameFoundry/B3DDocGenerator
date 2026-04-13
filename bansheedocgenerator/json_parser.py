"""JSON ingest for BansheeCodeGenerator's docgen output.

Reads a ``docgen.json`` file produced by the Clang-based BansheeCodeGenerator
and converts every entry into the ``RawDecl`` shape the IR builder already
consumes. This replaces the legacy line-based ``cpp_parser`` ingestion path.

Group scope (``@addtogroup`` / ``@defgroup``) and ``@name Internal`` ranges are
not present in the JSON — they are resolved by ``source_scanner`` from the
original headers and merged back in by the main build command.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from .model import DocBlock, EnumValue, RawDecl, SourceLoc


def parse_json(json_path: Path) -> list[RawDecl]:
	"""Load a BansheeCodeGenerator docgen.json file and return raw decls."""
	with open(json_path, "r", encoding="utf-8") as f:
		data = json.load(f)

	raws: list[RawDecl] = []
	for obj in data.get("classes", []):
		raws.append(_class_from_obj(obj))
	for obj in data.get("members", []):
		raws.append(_member_from_obj(obj))
	for obj in data.get("enums", []):
		raws.append(_enum_from_obj(obj))
	for obj in data.get("functions", []):
		raws.append(_function_from_obj(obj))
	return raws


# ---------------------------------------------------------------------------
# Decl-kind converters
# ---------------------------------------------------------------------------


def _class_from_obj(obj: dict) -> RawDecl:
	return RawDecl(
		kind=obj.get("kind", "class"),
		name=obj.get("name", ""),
		qualified_name=obj.get("qualified_name", ""),
		signature="",
		template_params=_strip_template_brackets(obj.get("template_params")),
		bases=list(obj.get("bases") or []),
		visibility=obj.get("visibility", "public"),
		namespace=obj.get("namespace", ""),
		doc=_doc_from_obj(obj.get("doc")),
		location=_location_from_obj(obj.get("location")),
	)


def _member_from_obj(obj: dict) -> RawDecl:
	kind = obj.get("kind", "method")
	return RawDecl(
		kind=kind,
		name=obj.get("name", ""),
		qualified_name=obj.get("qualified_name", ""),
		signature=obj.get("signature", ""),
		template_params=_strip_template_brackets(obj.get("template_params")),
		return_type=obj.get("return_type"),
		param_list=_param_list_from_obj(obj.get("param_list")),
		default_value=obj.get("default_value"),
		visibility=obj.get("visibility", "public"),
		is_static=bool(obj.get("is_static", False)),
		is_virtual=bool(obj.get("is_virtual", False)),
		is_const=bool(obj.get("is_const", False)),
		parent_class_qname=obj.get("parent_class_qname"),
		doc=_doc_from_obj(obj.get("doc")),
		location=_location_from_obj(obj.get("location")),
	)


def _enum_from_obj(obj: dict) -> RawDecl:
	values: list[EnumValue] = []
	for v in obj.get("enum_values") or []:
		values.append(
			EnumValue(
				name=v.get("name", ""),
				value=v.get("value"),
				doc=_doc_from_obj(v.get("doc")),
			)
		)
	return RawDecl(
		kind="enum",
		name=obj.get("name", ""),
		qualified_name=obj.get("qualified_name", ""),
		signature="",
		enum_values=values,
		enum_underlying=obj.get("enum_underlying"),
		is_enum_class=bool(obj.get("is_enum_class", False)),
		visibility=obj.get("visibility", "public"),
		namespace=obj.get("namespace", ""),
		doc=_doc_from_obj(obj.get("doc")),
		location=_location_from_obj(obj.get("location")),
	)


def _function_from_obj(obj: dict) -> RawDecl:
	return RawDecl(
		kind="function",
		name=obj.get("name", ""),
		qualified_name=obj.get("qualified_name", ""),
		signature=obj.get("signature", ""),
		template_params=_strip_template_brackets(obj.get("template_params")),
		return_type=obj.get("return_type"),
		param_list=_param_list_from_obj(obj.get("param_list")),
		visibility="public",
		namespace=obj.get("namespace", ""),
		doc=_doc_from_obj(obj.get("doc")),
		location=_location_from_obj(obj.get("location")),
	)


# ---------------------------------------------------------------------------
# Leaf converters
# ---------------------------------------------------------------------------


def _doc_from_obj(obj: Optional[dict]) -> DocBlock:
	if not obj:
		return DocBlock()
	return DocBlock(
		brief=obj.get("brief", "") or "",
		description=obj.get("description", "") or "",
		params=[(p[0], p[1]) for p in (obj.get("params") or []) if len(p) >= 2],
		template_params=[(p[0], p[1]) for p in (obj.get("template_params_doc") or []) if len(p) >= 2],
		returns=obj.get("returns", "") or "",
		copydoc_target=obj.get("copydoc_target"),
	)


def _location_from_obj(obj: Optional[dict]) -> Optional[SourceLoc]:
	if not obj:
		return None
	file = obj.get("file") or ""
	line = int(obj.get("line") or 0)
	if not file:
		return None
	return SourceLoc(file=_normalize_path(file), line=line)


def _param_list_from_obj(obj: Optional[list]) -> list[tuple[str, str]]:
	out: list[tuple[str, str]] = []
	for entry in obj or []:
		if len(entry) >= 2:
			out.append((entry[0], entry[1]))
	return out


def _strip_template_brackets(s: Any) -> Optional[str]:
	"""Convert ``"<class T>"`` to ``"class T"``.

	The legacy parser stored template parameter lists without the surrounding
	angle brackets (templates render as ``template<{{ value }}>``), so the IR
	and rendering code expects the inner text only. Clang's pretty-printer
	always wraps the list in ``<>``."""
	if not s:
		return None
	text = str(s).strip()
	if text.startswith("<") and text.endswith(">"):
		text = text[1:-1].strip()
	return text or None


# ---------------------------------------------------------------------------
# Path normalization
# ---------------------------------------------------------------------------


def _normalize_path(path: str) -> str:
	"""Canonicalize a path string so JSON locations and scanner paths match.

	The emitter stores whatever the compiler saw in ``#include`` resolution,
	which on Windows produces a mix of ``/`` and ``\\``. We fold the two and
	resolve to an absolute path when possible."""
	try:
		return str(Path(path.replace("\\", "/")).resolve()).replace("\\", "/")
	except OSError:
		return path.replace("\\", "/")
