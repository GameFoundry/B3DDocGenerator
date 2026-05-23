"""Source header scanner for things the JSON decl dump can't carry.

The BansheeCodeGenerator docgen JSON captures declarations and their attached
Doxygen comments, but it does **not** preserve scope-level Doxygen directives
that Clang ignores (``@defgroup``, ``@addtogroup``, ``@{``, ``@}``) or
``@name Internal`` blocks that mark a subset of class members as internal-only.
This module walks the original header files with a lightweight state machine
to produce:

	1. A flat ``GroupDecl`` list (fed into ``group_resolver``).
	2. A ``{normalized_path: [(start_line, end_line), ...]}`` map of line
	   ranges where ``@name Internal`` is in effect, which the main build
	   command uses to flip ``RawDecl.is_internal_name_block`` on the
	   affected members.
	3. A ``{normalized_path: [(group_name, start_line, end_line), ...]}``
	   map of group-scope spans, used to attach ``RawDecl.group_stack``
	   to every decl by looking up its source location.

No C++ parsing happens here — only Doxygen comment block extraction plus
marker matching. That's enough because the JSON already carries every decl's
file+line, so mapping back by location is trivial.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterator, Optional

from .json_parser import _normalize_path
from .model import GroupDecl, SourceLoc


_DEFGROUP_RE = re.compile(r"@defgroup\s+(\S+)\s+([^\n@]*(?:\n(?!\s*@)[^\n]*)*)", re.MULTILINE)
_ADDTOGROUP_RE = re.compile(r"@addtogroup\s+(\S+)")
_NAME_RE = re.compile(r"@name\s+(\S+)", re.IGNORECASE)
_STAR_PREFIX_RE = re.compile(r"^\s*\*\s?", re.MULTILINE)


# ----------------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------------


def scan_sources(
	header_paths: list[tuple[Path, str]],
) -> tuple[list[GroupDecl], dict[str, list[tuple[int, int]]], dict[str, list[tuple[str, int, int]]]]:
	"""Scan a batch of header files.

	Parameters
	----------
	header_paths : list of (absolute_path, repo_relative_posix)
		The same shape ``__main__._collect_headers`` produced for the old
		cpp_parser-based pipeline.

	Returns
	-------
	(group_decls, internal_ranges_by_file, group_spans_by_file)
		``internal_ranges_by_file`` and ``group_spans_by_file`` are both
		keyed by the canonical absolute posix path the JSON parser
		produces, so the two data sources can be cross-referenced by file.
	"""
	group_decls: list[GroupDecl] = []
	internal_ranges: dict[str, list[tuple[int, int]]] = {}
	group_spans: dict[str, list[tuple[str, int, int]]] = {}

	for abs_path, rel_path in header_paths:
		try:
			text = abs_path.read_text(encoding="utf-8", errors="replace")
		except OSError:
			continue

		scanner = _FileScanner(rel_path)
		scanner.scan(text)
		group_decls.extend(scanner.group_decls)
		key = _normalize_path(str(abs_path))
		if scanner.internal_ranges:
			internal_ranges[key] = scanner.internal_ranges
		if scanner.group_spans:
			group_spans[key] = scanner.group_spans

	return group_decls, internal_ranges, group_spans


# ----------------------------------------------------------------------------
# Doxygen comment tokenizer
# ----------------------------------------------------------------------------


def _iter_doc_comments(text: str) -> Iterator[tuple[str, int]]:
	"""Yield ``(cleaned_content, start_line)`` for each ``/** ... */`` block.

	Plain ``/* ... */`` comments, line comments, strings, and char literals are
	skipped so their contents can't accidentally trigger marker matches.
	Returned content has each continuation line's leading ``* `` stripped.
	"""
	i = 0
	n = len(text)
	line_no = 1

	while i < n:
		ch = text[i]
		if ch == "\n":
			line_no += 1
			i += 1
			continue
		if ch == '"':
			i += 1
			while i < n and text[i] != '"':
				if text[i] == "\\" and i + 1 < n:
					if text[i + 1] == "\n":
						line_no += 1
					i += 2
					continue
				if text[i] == "\n":
					line_no += 1
				i += 1
			if i < n:
				i += 1
			continue
		if ch == "'":
			i += 1
			while i < n and text[i] != "'":
				if text[i] == "\\" and i + 1 < n:
					i += 2
					continue
				if text[i] == "\n":
					line_no += 1
				i += 1
			if i < n:
				i += 1
			continue
		if ch == "/" and i + 1 < n:
			nxt = text[i + 1]
			if nxt == "/":
				while i < n and text[i] != "\n":
					i += 1
				continue
			if nxt == "*":
				is_doxy = i + 2 < n and text[i + 2] == "*"
				# /**/ is not a Doxygen block.
				if is_doxy and i + 3 < n and text[i + 3] == "/":
					is_doxy = False
				start_line = line_no
				i += 2
				content: list[str] = []
				while i < n:
					if text[i] == "*" and i + 1 < n and text[i + 1] == "/":
						i += 2
						break
					if text[i] == "\n":
						line_no += 1
					content.append(text[i])
					i += 1
				if is_doxy:
					raw = "".join(content)
					if raw.startswith("*"):
						raw = raw[1:]
					cleaned = _STAR_PREFIX_RE.sub("", raw).strip()
					yield cleaned, start_line
				continue
		i += 1


# ----------------------------------------------------------------------------
# Per-file state machine
# ----------------------------------------------------------------------------


class _FileScanner:
	def __init__(self, file_rel_path: str):
		self.file = file_rel_path
		self.group_decls: list[GroupDecl] = []
		# LIFO stack of open scopes — either a group or an @name Internal block.
		# Groups: ("group", name, open_line)
		# Internal names: ("internal", start_line)
		# Non-internal names don't push — they just close any open internal frame.
		self.scope_stack: list[tuple] = []
		self.internal_ranges: list[tuple[int, int]] = []
		self.group_spans: list[tuple[str, int, int]] = []
		self._last_line = 1

	def scan(self, text: str) -> None:
		self._last_line = text.count("\n") + 1
		for cleaned, line_no in _iter_doc_comments(text):
			self._handle_comment(cleaned, line_no)
		# Flush any scopes still open at EOF. Dangling internal ranges get a
		# minimal fallback rather than extending to EOF; dangling group spans
		# extend to EOF so decls past the last @} still pick up the group.
		for frame in self.scope_stack:
			if frame[0] == "internal":
				self.internal_ranges.append((frame[1], frame[1]))
			elif frame[0] == "group":
				self.group_spans.append((frame[1], frame[2], self._last_line))

	def _handle_comment(self, cleaned: str, line_no: int) -> None:
		has_open = "@{" in cleaned
		has_close = "@}" in cleaned

		defgroup_match = _DEFGROUP_RE.search(cleaned)
		addtogroup_match = _ADDTOGROUP_RE.search(cleaned) if not defgroup_match else None
		name_match = _NAME_RE.search(cleaned)

		if defgroup_match:
			name = defgroup_match.group(1)
			rest = defgroup_match.group(2).strip()
			title, description = _split_title_description(rest)
			parent_stack = self._group_parents()
			self.group_decls.append(
				GroupDecl(
					name=name,
					title=title or name,
					description=description,
					kind="defgroup",
					parent_stack=parent_stack,
					location=SourceLoc(self.file, line_no),
				)
			)
			if has_open:
				self.scope_stack.append(("group", name, line_no))
			return

		if addtogroup_match:
			name = addtogroup_match.group(1)
			parent_stack = self._group_parents()
			self.group_decls.append(
				GroupDecl(
					name=name,
					kind="addtogroup",
					parent_stack=parent_stack,
					location=SourceLoc(self.file, line_no),
				)
			)
			if has_open:
				self.scope_stack.append(("group", name, line_no))
			return

		if name_match:
			arg = name_match.group(1).strip()
			self._close_open_internal(line_no)
			if arg.lower() == "internal":
				self.scope_stack.append(("internal", line_no))
			# Fall through so a comment like ``@name Internal @{`` still handles
			# a trailing ``@}`` if anyone writes it.

		if has_close and not has_open:
			# Close exactly one scope — the innermost. A single @} matches a
			# single @{, whether it opened an internal (@name) or a group
			# (@addtogroup / @defgroup) frame. Dangling frames get handled by
			# the EOF flush in ``scan``.
			if self.scope_stack:
				top = self.scope_stack.pop()
				if top[0] == "internal":
					self.internal_ranges.append((top[1], line_no))
				elif top[0] == "group":
					self.group_spans.append((top[1], top[2], line_no))

	def _close_open_internal(self, line_no: int) -> None:
		for idx in range(len(self.scope_stack) - 1, -1, -1):
			if self.scope_stack[idx][0] == "internal":
				_, start = self.scope_stack.pop(idx)
				self.internal_ranges.append((start, line_no))
				return

	def _group_parents(self) -> list[str]:
		return [frame[1] for frame in self.scope_stack if frame[0] == "group"]


def _split_title_description(rest: str) -> tuple[str, str]:
	if "\n" in rest:
		first, remainder = rest.split("\n", 1)
		return first.strip(), remainder.strip()
	return rest.strip(), ""
