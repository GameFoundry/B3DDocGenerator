"""Data model for BansheeDocGenerator.

These dataclasses are the intermediate representation shared by every phase of
the build: the parsers emit them, the renderers consume them. Keep them free of
render-layer concerns (no HTML, no Jinja context).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

Visibility = str  # "public" | "protected" | "private" | "internal"
Kind = str  # "class" | "struct" | "enum" | "enum_value" | "method" | "field" |
#           # "function" | "typedef" | "macro" | "variable"


@dataclass
class SourceLoc:
	file: str  # repo-relative posix path
	line: int


@dataclass
class DocBlock:
	brief: str = ""
	description: str = ""  # Markdown body, pre-render
	params: list[tuple[str, str]] = field(default_factory=list)  # (name, description)
	returns: str = ""
	notes: list[str] = field(default_factory=list)
	see_also: list[str] = field(default_factory=list)
	copydoc_target: Optional[str] = None
	raw: str = ""  # raw comment text for debugging/fallback


@dataclass
class Member:
	kind: Kind
	name: str
	qualified_name: str
	signature: str  # canonical one-line rendering
	anchor: str = ""
	visibility: Visibility = "public"
	is_internal_name_block: bool = False
	is_static: bool = False
	is_virtual: bool = False
	is_const: bool = False
	template_params: Optional[str] = None
	return_type: Optional[str] = None
	param_list: list[tuple[str, str]] = field(default_factory=list)  # (type, name)
	default_value: Optional[str] = None
	doc: DocBlock = field(default_factory=DocBlock)
	location: Optional[SourceLoc] = None
	overload_index: int = 0


@dataclass
class EnumValue:
	name: str
	value: Optional[str] = None
	doc: DocBlock = field(default_factory=DocBlock)


@dataclass
class Class:
	kind: Kind  # "class" or "struct"
	name: str
	qualified_name: str
	template_params: Optional[str]
	bases: list[str]
	namespace: str
	group_names: list[str] = field(default_factory=list)
	is_internal: bool = False
	members: list[Member] = field(default_factory=list)
	doc: DocBlock = field(default_factory=DocBlock)
	location: Optional[SourceLoc] = None
	url: str = ""


@dataclass
class Enum:
	name: str
	qualified_name: str
	underlying: Optional[str] = None
	is_class_enum: bool = False
	values: list[EnumValue] = field(default_factory=list)
	namespace: str = ""
	group_names: list[str] = field(default_factory=list)
	is_internal: bool = False
	doc: DocBlock = field(default_factory=DocBlock)
	location: Optional[SourceLoc] = None
	url: str = ""


@dataclass
class FreeFunction:
	name: str
	qualified_name: str
	signature: str
	return_type: Optional[str] = None
	param_list: list[tuple[str, str]] = field(default_factory=list)
	template_params: Optional[str] = None
	namespace: str = ""
	group_names: list[str] = field(default_factory=list)
	is_internal: bool = False
	doc: DocBlock = field(default_factory=DocBlock)
	location: Optional[SourceLoc] = None
	url: str = ""
	anchor: str = ""
	overload_index: int = 0


@dataclass
class Group:
	name: str
	title: str
	description: str = ""
	parent: Optional[str] = None
	children: list[str] = field(default_factory=list)
	classes: list[str] = field(default_factory=list)  # qualified names
	enums: list[str] = field(default_factory=list)
	functions: list[str] = field(default_factory=list)  # anchors/qualified names
	is_internal: bool = False
	defined_in: Optional[SourceLoc] = None
	order: int = 0


@dataclass
class Manual:
	slug: str  # e.g. "00_User_Manuals/04_Rendering/00_cameras"
	title: str
	order_key: tuple
	source_path: str
	parent_slug: Optional[str] = None
	children: list[str] = field(default_factory=list)
	headings: list[tuple[int, str, str]] = field(default_factory=list)  # (level, text, anchor)
	html: str = ""  # post-render


@dataclass
class ManualTreeNode:
	"""One entry in the manual navigation tree.

	A node is either a manual page (slug set) or a directory grouping
	(slug is None — dir_path identifies it).
	"""
	title: str
	slug: Optional[str] = None  # None for directory-only nodes
	dir_path: str = ""  # posix directory path relative to docs root
	order_key: tuple = ()
	children: list["ManualTreeNode"] = field(default_factory=list)


@dataclass
class SymbolEntry:
	qualified_name: str
	kind: Kind
	url: str
	is_internal: bool = False


@dataclass
class Site:
	groups: dict[str, Group] = field(default_factory=dict)
	classes: dict[str, Class] = field(default_factory=dict)
	enums: dict[str, Enum] = field(default_factory=dict)
	functions: dict[str, FreeFunction] = field(default_factory=dict)
	manuals: dict[str, Manual] = field(default_factory=dict)
	manual_tree: list["ManualTreeNode"] = field(default_factory=list)
	symbol_index: dict[str, list[SymbolEntry]] = field(default_factory=dict)
	root_group_order: list[str] = field(default_factory=list)
	root_manual_order: list[str] = field(default_factory=list)


# ------------------------------- Raw parser output ----------------------------

@dataclass
class RawDecl:
	"""One raw declaration emitted by cpp_parser before IR normalization."""

	kind: Kind
	name: str
	qualified_name: str
	signature: str = ""
	template_params: Optional[str] = None
	return_type: Optional[str] = None
	param_list: list[tuple[str, str]] = field(default_factory=list)
	bases: list[str] = field(default_factory=list)
	enum_values: list[EnumValue] = field(default_factory=list)
	enum_underlying: Optional[str] = None
	is_enum_class: bool = False
	default_value: Optional[str] = None
	visibility: Visibility = "public"
	is_internal_name_block: bool = False
	is_static: bool = False
	is_virtual: bool = False
	is_const: bool = False
	parent_class_qname: Optional[str] = None  # for members
	namespace: str = ""
	group_stack: list[str] = field(default_factory=list)
	doc: DocBlock = field(default_factory=DocBlock)
	location: Optional[SourceLoc] = None


@dataclass
class GroupDecl:
	"""A @defgroup or @addtogroup occurrence with hierarchy context."""

	name: str
	title: str = ""
	description: str = ""
	kind: str = "addtogroup"  # "defgroup" or "addtogroup"
	parent_stack: list[str] = field(default_factory=list)
	location: Optional[SourceLoc] = None
