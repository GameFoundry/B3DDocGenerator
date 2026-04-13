"""Lightweight line/character-based C++ header parser.

Extracts Doxygen /** */ comment blocks, associated declarations (classes,
structs, enums, methods, fields, free functions), namespace context, and
@addtogroup / @defgroup scope.

This is not a full C++ parser. It handles Banshee's coding style and the
vast majority of Doxygen patterns in use. When in doubt it skips rather than
raising.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

from .config import DECL_PREFIX_SKIP_MACROS, DECL_PREFIX_SKIP_WORDS
from .model import DocBlock, EnumValue, GroupDecl, RawDecl, SourceLoc
from .util import vlog, warn


# ----------------------------------------------------------------------------
# Tokenizer — splits a file into a stream of (kind, text, line_no) events.
# kind is one of:
#   "comment"  — full /** ... */ block content (leading /** and trailing */
#                are stripped; '*' prefixes on continuation lines are kept)
#   "code"     — a chunk of non-comment source (with // comments removed
#                and preprocessor directives elided)
# ----------------------------------------------------------------------------


def _tokenize(text: str) -> Iterator[tuple[str, str, int]]:
	i = 0
	n = len(text)
	line_no = 1
	code_buf: list[str] = []
	code_line_start = 1

	def flush_code():
		nonlocal code_buf, code_line_start
		if code_buf:
			yield_val = ("code", "".join(code_buf), code_line_start)
			code_buf = []
			code_line_start = line_no
			return yield_val
		return None

	while i < n:
		ch = text[i]
		# Line break
		if ch == "\n":
			code_buf.append(ch)
			line_no += 1
			i += 1
			continue
		# Block/line comment?
		if ch == "/" and i + 1 < n:
			nxt = text[i + 1]
			if nxt == "/":
				# Line comment — drop to end-of-line (but preserve the newline)
				while i < n and text[i] != "\n":
					i += 1
				continue
			if nxt == "*":
				# Flush any accumulated code
				flushed = None
				if code_buf:
					flushed = ("code", "".join(code_buf), code_line_start)
					code_buf = []
				# Determine if this is a Doxygen /** ... */ or a plain /* ... */
				is_doxy = i + 2 < n and text[i + 2] == "*"
				# But careful: /**/ (empty) is not doxy
				if is_doxy and i + 3 < n and text[i + 3] == "/":
					is_doxy = False
				comment_start_line = line_no
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
				if flushed is not None:
					yield flushed
					code_line_start = line_no
				if is_doxy:
					# Drop the leading '*' that comes right after /** in /**...
					# content_str already excludes the '/**' — but the first char may be '*'
					content_str = "".join(content)
					if content_str.startswith("*"):
						content_str = content_str[1:]
					yield ("comment", content_str, comment_start_line)
				# Non-doxy block comments are simply discarded.
				code_line_start = line_no
				continue
		# String literal
		if ch == '"':
			code_buf.append(ch)
			i += 1
			while i < n:
				if text[i] == "\\" and i + 1 < n:
					code_buf.append(text[i])
					code_buf.append(text[i + 1])
					i += 2
					continue
				if text[i] == '"':
					code_buf.append(text[i])
					i += 1
					break
				if text[i] == "\n":
					line_no += 1
				code_buf.append(text[i])
				i += 1
			continue
		# Char literal
		if ch == "'":
			code_buf.append(ch)
			i += 1
			while i < n:
				if text[i] == "\\" and i + 1 < n:
					code_buf.append(text[i])
					code_buf.append(text[i + 1])
					i += 2
					continue
				if text[i] == "'":
					code_buf.append(text[i])
					i += 1
					break
				code_buf.append(text[i])
				i += 1
			continue
		# Preprocessor line — drop to end of logical line (handle \\\n continuations)
		if ch == "#":
			# Ensure we're at the start of a line (after optional whitespace)
			j = len(code_buf) - 1
			at_line_start = True
			while j >= 0 and code_buf[j] != "\n":
				if not code_buf[j].isspace():
					at_line_start = False
					break
				j -= 1
			if at_line_start:
				while i < n:
					if text[i] == "\\" and i + 1 < n and text[i + 1] == "\n":
						i += 2
						line_no += 1
						continue
					if text[i] == "\n":
						code_buf.append("\n")
						line_no += 1
						i += 1
						break
					i += 1
				continue
		code_buf.append(ch)
		i += 1

	if code_buf:
		yield ("code", "".join(code_buf), code_line_start)


# ----------------------------------------------------------------------------
# Doxygen comment parsing
# ----------------------------------------------------------------------------


_STAR_PREFIX_RE = re.compile(r"^[ \t]*\*[ \t]?", re.MULTILINE)


def _clean_comment(raw: str) -> str:
	"""Strip leading '*' markers on each line of a block comment."""
	return _STAR_PREFIX_RE.sub("", raw).strip()


_DEFGROUP_RE = re.compile(r"@defgroup\s+(\S+)\s*(.*)")
_ADDTOGROUP_RE = re.compile(r"@addtogroup\s+(\S+)")


def _parse_doc_comment(raw: str) -> DocBlock:
	"""Parse a cleaned comment body into a DocBlock."""
	cleaned = _clean_comment(raw)
	doc = DocBlock(raw=cleaned)
	# Strip out group directives (handled separately).
	cleaned = re.sub(r"@(defgroup|addtogroup)[^\n]*\n?", "", cleaned)
	cleaned = re.sub(r"@\{", "", cleaned)
	cleaned = re.sub(r"@\}", "", cleaned)
	# @p foo -> `foo` (Doxygen parameter reference)
	cleaned = re.sub(r"@p\s+([A-Za-z_]\w*)", r"`\1`", cleaned)

	lines = cleaned.split("\n")
	# Split into paragraphs by blank lines to preserve structure. Tag directives
	# ('@param foo ...', '@return ...', etc.) may span multiple lines; subsequent
	# non-empty lines are appended to the current tag until a blank line or the
	# next recognized tag.
	body_lines: list[str] = []
	current_tag: str | None = None  # 'param' | 'return' | 'note'
	def append_to_current(text: str) -> None:
		text = text.strip()
		if not text:
			return
		if current_tag == "param" and doc.params:
			key, val = doc.params[-1]
			sep = " " if val else ""
			doc.params[-1] = (key, f"{val}{sep}{text}")
		elif current_tag == "return":
			doc.returns = f"{doc.returns} {text}" if doc.returns else text
		elif current_tag == "note" and doc.notes:
			sep = " " if doc.notes[-1] else ""
			doc.notes[-1] = f"{doc.notes[-1]}{sep}{text}"
	for line in lines:
		stripped = line.strip()
		if stripped == "":
			current_tag = None
			body_lines.append(line)
			continue
		m_param = re.match(r"@param(?:\[[^\]]*\])?\s+(\S+)(?:\s+(.*))?$", stripped)
		m_ret = re.match(r"@(?:return|returns)\b\s*(.*)$", stripped)
		m_note = re.match(r"@note\b\s*(.*)$", stripped)
		m_see = re.match(r"@(?:see|sa)\b\s*(.*)$", stripped)
		m_copydoc = re.match(r"@copydoc\s+(\S+)", stripped)
		m_brief = re.match(r"@brief\b\s*(.*)$", stripped)
		if m_param:
			doc.params.append((m_param.group(1), m_param.group(2) or ""))
			current_tag = "param"
		elif m_ret:
			doc.returns = m_ret.group(1)
			current_tag = "return"
		elif m_note:
			doc.notes.append(m_note.group(1))
			current_tag = "note"
		elif m_see:
			doc.see_also.append(m_see.group(1))
			current_tag = None
		elif m_copydoc:
			doc.copydoc_target = m_copydoc.group(1)
			current_tag = None
		elif m_brief:
			body_lines.append(m_brief.group(1))
			current_tag = None
		elif current_tag is not None:
			append_to_current(stripped)
		else:
			body_lines.append(line)

	# Javadoc auto-brief: the first sentence (up to the first '. ' / end-of-line
	# dot) of the first non-empty text line is the brief; the rest — including
	# the remainder of that paragraph — becomes the description.
	paragraphs: list[list[str]] = [[]]
	for ln in body_lines:
		if ln.strip() == "":
			if paragraphs[-1]:
				paragraphs.append([])
		else:
			paragraphs[-1].append(ln.rstrip())
	paragraphs = [p for p in paragraphs if p]
	if paragraphs:
		first_para_text = "\n".join(paragraphs[0])
		sentence_end = re.search(r"\.(?:\s|$)", first_para_text)
		if sentence_end:
			end_idx = sentence_end.end()
			doc.brief = re.sub(r"\s+", " ", first_para_text[:end_idx]).strip()
			leftover = first_para_text[end_idx:].strip("\n")
		else:
			doc.brief = re.sub(r"\s+", " ", first_para_text).strip()
			leftover = ""
		desc_parts: list[str] = []
		if leftover:
			desc_parts.append(leftover)
		for p in paragraphs[1:]:
			desc_parts.append("\n".join(p))
		if desc_parts:
			doc.description = "\n\n".join(desc_parts).strip()
	return doc


def _extract_group_markers(raw: str) -> dict:
	"""Return dict with keys defgroup, addtogroup, open_brace, close_brace."""
	cleaned = _clean_comment(raw)
	result = {
		"defgroup": None,  # (name, title, description)
		"addtogroup": None,
		"open_brace": False,
		"close_brace": False,
	}
	m = _DEFGROUP_RE.search(cleaned)
	if m:
		name = m.group(1)
		rest = m.group(2).strip()
		# Rest is "Title\nDescription..."
		title = rest.split("\n", 1)[0].strip()
		description = ""
		if "\n" in rest:
			description = rest.split("\n", 1)[1].strip()
		result["defgroup"] = (name, title, description)
	m = _ADDTOGROUP_RE.search(cleaned)
	if m:
		result["addtogroup"] = m.group(1)
	if "@{" in cleaned:
		result["open_brace"] = True
	if "@}" in cleaned:
		result["close_brace"] = True
	return result


# ----------------------------------------------------------------------------
# Declaration scanner
# ----------------------------------------------------------------------------


_NAMESPACE_RE = re.compile(r"\bnamespace\s+([A-Za-z_]\w*)\s*\{")
_NAMESPACE_ANON_RE = re.compile(r"\bnamespace\s*\{")


@dataclass
class _ClassCtx:
	name: str
	qualified_name: str
	kind: str  # "class" or "struct"
	entry_brace_depth: int
	visibility: str
	in_internal_name_block: bool = False
	pending_comment: Optional[DocBlock] = None
	nested_enums: list[RawDecl] = None  # captured separately
	# True for partial template specializations of a primary template — we
	# still need to track the scope so the body doesn't leak as free functions,
	# but we don't emit a duplicate class entry or any of its members. The
	# canonical entry comes from the primary template declaration.
	is_shadow: bool = False

	def __post_init__(self):
		if self.nested_enums is None:
			self.nested_enums = []


@dataclass
class _NamespaceCtx:
	name: str  # empty for anonymous
	entry_brace_depth: int
	# True when this frame is a synthetic outer segment of a C++17 nested
	# namespace declaration (``namespace a::b::c``). Only the innermost frame
	# owns the closing ``}``; synthetic frames must pop together with it.
	synthetic: bool = False


class _FileParser:
	def __init__(self, file_rel_path: str):
		self.file = file_rel_path
		self.raw_decls: list[RawDecl] = []
		self.group_decls: list[GroupDecl] = []
		self.namespace_stack: list[_NamespaceCtx] = []
		self.class_stack: list[_ClassCtx] = []
		self.brace_depth = 0
		self.group_stack: list[tuple[str, int]] = []  # (group_name, open_count_level)
		self.top_pending_comment: Optional[DocBlock] = None

	# -- utility ------------------------------------------------------------

	def _current_namespace(self) -> str:
		parts = [ns.name for ns in self.namespace_stack if ns.name]
		return "::".join(parts)

	def _qualify(self, name: str) -> str:
		ns = self._current_namespace()
		scope_parts = []
		if ns:
			scope_parts.append(ns)
		for cls in self.class_stack:
			scope_parts.append(cls.name)
		scope_parts.append(name)
		return "::".join(scope_parts)

	def _group_snapshot(self) -> list[str]:
		return [g for g, _ in self.group_stack]

	def _consume_pending(self) -> Optional[DocBlock]:
		if self.class_stack and self.class_stack[-1].pending_comment is not None:
			doc = self.class_stack[-1].pending_comment
			self.class_stack[-1].pending_comment = None
			return doc
		doc = self.top_pending_comment
		self.top_pending_comment = None
		return doc

	def _set_pending(self, doc: DocBlock) -> None:
		if self.class_stack:
			self.class_stack[-1].pending_comment = doc
		else:
			self.top_pending_comment = doc

	# -- comment handling ---------------------------------------------------

	def handle_comment(self, raw_content: str, line_no: int) -> None:
		# Trailing doc comment (`/**< ... */`) — attach to the last emitted decl.
		if raw_content.lstrip().startswith("<"):
			trailing_body = raw_content.lstrip()[1:]
			doc = _parse_doc_comment(trailing_body)
			if self.raw_decls:
				last = self.raw_decls[-1]
				# Only attach if the last decl was within the current class (if any).
				if not self.class_stack or last.parent_class_qname == self.class_stack[-1].qualified_name:
					last.doc = doc
			return

		markers = _extract_group_markers(raw_content)
		has_group_directive = markers["defgroup"] or markers["addtogroup"]

		if markers["defgroup"]:
			name, title, description = markers["defgroup"]
			parent_stack = self._group_snapshot()
			gd = GroupDecl(
				name=name,
				title=title or name,
				description=description,
				kind="defgroup",
				parent_stack=parent_stack,
				location=SourceLoc(self.file, line_no),
			)
			self.group_decls.append(gd)
			# If the comment also opens a brace, push onto the group stack
			if markers["open_brace"]:
				self.group_stack.append((name, 1))
		elif markers["addtogroup"]:
			name = markers["addtogroup"]
			if markers["open_brace"]:
				self.group_stack.append((name, 1))
			# Register an addtogroup reference so the resolver knows this name is used.
			parent_stack = self._group_snapshot()[:-1] if self.group_stack else []
			self.group_decls.append(
				GroupDecl(
					name=name,
					kind="addtogroup",
					parent_stack=parent_stack,
					location=SourceLoc(self.file, line_no),
				)
			)

		if markers["close_brace"] and not markers["open_brace"]:
			# Pop the innermost group scope, or close @name Internal block if
			# we're currently inside one (the two cases are distinguished by
			# whether we're inside a class body).
			if self.class_stack and self.class_stack[-1].in_internal_name_block:
				self.class_stack[-1].in_internal_name_block = False
			elif self.group_stack:
				self.group_stack.pop()

		if has_group_directive:
			return

		# Normal doc comment — parse and stash as pending.
		doc = _parse_doc_comment(raw_content)
		self._set_pending(doc)

	# -- code handling ------------------------------------------------------

	def handle_code(self, text: str, start_line: int) -> None:
		# Walk the code chunk by character, tracking braces and statements.
		# At statement boundaries try to recognize declarations.
		i = 0
		n = len(text)
		line = start_line
		stmt_buf: list[str] = []
		stmt_line_start = start_line

		def emit_stmt(trailing: str) -> None:
			nonlocal stmt_buf, stmt_line_start
			stmt = "".join(stmt_buf) + trailing
			stmt_stripped = stmt.strip()
			if stmt_stripped:
				self._handle_statement(stmt_stripped, stmt_line_start)
			stmt_buf = []
			stmt_line_start = line

		while i < n:
			ch = text[i]
			if ch == "\n":
				line += 1
				stmt_buf.append(ch)
				i += 1
				continue
			if ch == '"' or ch == "'":
				# String/char literals already stripped? No — tokenizer keeps them in code chunks.
				# Skip to matching end.
				quote = ch
				stmt_buf.append(ch)
				i += 1
				while i < n:
					if text[i] == "\\" and i + 1 < n:
						stmt_buf.append(text[i])
						stmt_buf.append(text[i + 1])
						i += 2
						continue
					if text[i] == quote:
						stmt_buf.append(text[i])
						i += 1
						break
					if text[i] == "\n":
						line += 1
					stmt_buf.append(text[i])
					i += 1
				continue
			if ch == "{":
				# Try to match an opener declaration before consuming brace
				stmt_text = "".join(stmt_buf).strip()
				if self._try_open_scope(stmt_text, stmt_line_start):
					self.brace_depth += 1
					i += 1
					stmt_buf = []
					stmt_line_start = line
					continue
				# Otherwise: this is a function body / initializer list. Emit the
				# preceding text as a declaration statement, then skip the body.
				emit_stmt("")
				self.brace_depth += 1
				depth_target = self.brace_depth - 1
				i += 1
				while i < n and self.brace_depth > depth_target:
					c = text[i]
					if c == "\n":
						line += 1
					elif c == "{":
						self.brace_depth += 1
					elif c == "}":
						self.brace_depth -= 1
						if self.brace_depth == depth_target:
							i += 1
							break
					elif c == '"' or c == "'":
						q = c
						i += 1
						while i < n:
							if text[i] == "\\" and i + 1 < n:
								i += 2
								continue
							if text[i] == q:
								i += 1
								break
							if text[i] == "\n":
								line += 1
							i += 1
						continue
					i += 1
				# After skipping body, stmt_buf is empty; continue reading
				stmt_line_start = line
				continue
			if ch == "}":
				# Close a scope
				self.brace_depth -= 1
				# Emit any pending statement text first (ignored unless it's a decl on its own)
				if stmt_buf:
					emit_stmt("")
				# Pop namespace/class contexts whose body has now ended
				while self.class_stack and self.class_stack[-1].entry_brace_depth >= self.brace_depth:
					self.class_stack.pop()
				while self.namespace_stack and self.namespace_stack[-1].entry_brace_depth >= self.brace_depth:
					self.namespace_stack.pop()
				i += 1
				continue
			if ch == ":":
				# Access specifier (public:/protected:/private:) — emit pseudo-stmt.
				pending_text = "".join(stmt_buf).strip()
				m_access = re.match(r"^(public|protected|private)$", pending_text)
				if m_access and self.class_stack:
					self.class_stack[-1].visibility = m_access.group(1)
					# Any lingering @name Internal section is closed when the
					# access cursor moves.
					self.class_stack[-1].in_internal_name_block = False
					stmt_buf = []
					stmt_line_start = line
					i += 1
					continue
				# Otherwise fall through (inheritance list colon, bitfield, ?: etc.)
				stmt_buf.append(ch)
				i += 1
				continue
			if ch == ";":
				emit_stmt(";")
				i += 1
				continue
			stmt_buf.append(ch)
			i += 1

		# Any trailing content is ignored (typically whitespace).

	# -- statement handling -------------------------------------------------

	def _handle_statement(self, stmt: str, line_no: int) -> None:
		# Strip trailing semicolon for matching.
		s = stmt.rstrip(";").strip()
		if not s:
			return
		# Access specifier lines (may come with trailing comment that was stripped upstream)
		ms = re.match(r"^(public|protected|private)\s*:\s*$", s)
		if ms and self.class_stack:
			self.class_stack[-1].visibility = ms.group(1)
			# An access specifier closes any lingering @name Internal section? Keep it off.
			# We model @name Internal as a flag — the next comment reopens it if needed.
			return
		# `using` / `typedef` — skip (don't produce pages, but could be added later)
		if re.match(r"^(using|typedef)\b", s):
			self._consume_pending()  # drop any attached comment
			return
		# `friend` — skip
		if re.match(r"^friend\b", s):
			self._consume_pending()
			return
		# Free-function or field at namespace scope OR method/field at class scope
		if self.class_stack:
			self._handle_class_member(s, line_no)
		else:
			self._handle_namespace_scope_decl(s, line_no)

	# -- scope openers ------------------------------------------------------

	def _try_open_scope(self, text: str, line_no: int) -> bool:
		"""If `text` is a namespace / class / struct / enum declaration, enter it.

		Returns True if a scope was opened (caller should advance past the '{').
		"""
		s = text.strip()
		if not s:
			return False

		# namespace foo  OR  namespace foo::bar::baz (C++17 nested namespaces).
		# Only one brace opens, so we record the inner-most frame as the one
		# that owns the closing brace depth; the outer segments are synthetic
		# frames that close together with it.
		m = re.match(r"^namespace\s+([A-Za-z_]\w*(?:\s*::\s*[A-Za-z_]\w*)*)\s*$", s)
		if m:
			segments = [seg.strip() for seg in m.group(1).split("::")]
			for seg in segments[:-1]:
				self.namespace_stack.append(
					_NamespaceCtx(seg, self.brace_depth, synthetic=True)
				)
			self.namespace_stack.append(
				_NamespaceCtx(segments[-1], self.brace_depth)
			)
			self._consume_pending()  # namespace doc comment dropped
			return True
		if re.match(r"^namespace\s*$", s):
			self.namespace_stack.append(_NamespaceCtx("", self.brace_depth))
			self._consume_pending()
			return True

		# enum [class] Name : Type
		m = re.match(
			r"^enum\s+(class\s+)?(?:[A-Z_][\w()]*\s+)*([A-Za-z_]\w*)\s*(?::\s*([^\{]+))?\s*$",
			s,
		)
		if m:
			is_class_enum = bool(m.group(1))
			name = m.group(2)
			underlying = (m.group(3) or "").strip() or None
			self._open_enum(name, is_class_enum, underlying, line_no)
			return True

		# class/struct [any-decorations] Name [: bases]
		# Strip an optional balanced template<...> prefix first; the angle-bracket
		# walker handles nested templates that the old `[^>]*` regex couldn't.
		template_params, after_tpl = _split_template_prefix(s)
		m = re.match(r"^(class|struct)\b([^{]*)$", after_tpl)
		if m:
			kind = m.group(1)
			rest = m.group(2).strip()
			# Split off optional inheritance list first so it isn't scanned for tokens.
			bases_str = ""
			colon_i = _find_top_level_colon(rest)
			if colon_i != -1:
				bases_str = rest[colon_i + 1:].strip()
				rest = rest[:colon_i].strip()
			# A partial template specialization carries trailing `<...>` after
			# the class name (e.g. ``TGroup<TOwnedTypes<>, ...>``). Strip it
			# with bracket counting before pulling the simple name out.
			rest_no_args = _strip_trailing_template_args(rest)
			is_specialization = rest_no_args != rest
			# Drop the optional ``final`` modifier so it isn't picked up as
			# the class name. Without this every ``struct Foo final`` ends up
			# documented as a class literally named ``final``.
			rest_no_args = re.sub(r"\bfinal\s*$", "", rest_no_args).strip()
			m2 = re.search(r"([A-Za-z_]\w*)\s*$", rest_no_args)
			if not m2:
				return False
			name = m2.group(1)
			if is_specialization:
				# Open an opaque scope so the specialization body is consumed
				# alongside any nested doc comments — without emitting a
				# duplicate class entry or any of its members. The primary
				# template declaration remains the canonical documentation.
				self._consume_pending()
				ctx = _ClassCtx(
					name=name,
					qualified_name=self._qualify(name),
					kind=kind,
					entry_brace_depth=self.brace_depth,
					visibility="public" if kind == "struct" else "private",
					is_shadow=True,
				)
				self.class_stack.append(ctx)
				return True
			bases = [b.strip() for b in bases_str.split(",")] if bases_str else []
			qname = self._qualify(name)
			ctx = _ClassCtx(
				name=name,
				qualified_name=qname,
				kind=kind,
				entry_brace_depth=self.brace_depth,
				visibility="public" if kind == "struct" else "private",
			)
			doc = self._consume_pending()
			decl = RawDecl(
				kind=kind,
				name=name,
				qualified_name=qname,
				template_params=template_params,
				bases=bases,
				namespace=self._current_namespace(),
				group_stack=self._group_snapshot(),
				doc=doc or DocBlock(),
				location=SourceLoc(self.file, line_no),
			)
			self.raw_decls.append(decl)
			self.class_stack.append(ctx)
			return True

		return False

	# -- enums --------------------------------------------------------------

	def _open_enum(
		self,
		name: str,
		is_class_enum: bool,
		underlying: Optional[str],
		line_no: int,
	) -> None:
		# Enums close without opening a class scope — we parse their body
		# greedily as part of this call via a mini-parser driven by handle_code.
		# To keep state simple, represent enum as a pseudo-class context that
		# _handle_class_member will redirect into enum-value mode.
		doc = self._consume_pending()
		qname = self._qualify(name)
		decl = RawDecl(
			kind="enum",
			name=name,
			qualified_name=qname,
			namespace=self._current_namespace(),
			group_stack=self._group_snapshot(),
			is_enum_class=is_class_enum,
			enum_underlying=underlying,
			doc=doc or DocBlock(),
			location=SourceLoc(self.file, line_no),
		)
		self.raw_decls.append(decl)
		# Push a special enum context so subsequent statements go into enum_values.
		ctx = _ClassCtx(
			name=name,
			qualified_name=qname,
			kind="enum",
			entry_brace_depth=self.brace_depth,
			visibility="public",
		)
		ctx.nested_enums = [decl]  # reuse field as backref
		self.class_stack.append(ctx)

	def _handle_class_member(self, stmt: str, line_no: int) -> None:
		ctx = self.class_stack[-1]
		if ctx.is_shadow:
			# Body of a partial specialization — consume any pending doc and
			# drop the statement on the floor. We only kept the scope to stop
			# the body from leaking out as namespace-level free functions.
			self._consume_pending()
			return
		if ctx.kind == "enum":
			# Enum values: a comma-separated list may come in one statement, but
			# our tokenizer splits on ';' only. So values come through as one big
			# statement (e.g. "PCM, VORBIS"). Parse them.
			parent_decl = ctx.nested_enums[0]
			pending = self._consume_pending()
			# Handle each comma-separated value
			parts = _split_top_level(stmt, ",")
			for part_idx, part in enumerate(parts):
				part = part.strip()
				if not part:
					continue
				# Remove trailing inline doc like `Foo /**< ... */`
				inline_doc = None
				m_inline = re.search(r"/\*\*<(.*)$", part)
				# Note: /**< comments were stripped by tokenizer — this is best effort.
				# Strip macro decorations (e.g. B3D_SCRIPT_EXPORT(Exclude(true)))
				part = _strip_prefix_decorations(part)
				m = re.match(r"^([A-Za-z_]\w*)\s*(?:=\s*(.+))?$", part)
				if not m:
					continue
				val_name = m.group(1)
				val_expr = m.group(2)
				doc = pending if part_idx == 0 else DocBlock()
				parent_decl.enum_values.append(
					EnumValue(name=val_name, value=val_expr, doc=doc or DocBlock())
				)
			return

		# A class member — could be a method or a field.
		doc = self._consume_pending()
		decl = self._match_member_decl(stmt, line_no, ctx, doc)
		if decl is not None:
			self.raw_decls.append(decl)

	def _handle_namespace_scope_decl(self, stmt: str, line_no: int) -> None:
		doc = self._consume_pending()
		# Strip macro decorations up front so things like
		# `B3D_SCRIPT_EXPORT(...)` with no trailing declaration don't leak
		# through to the function matcher.
		stripped = _strip_prefix_decorations(re.sub(r"\s+", " ", stmt).strip())
		if not stripped:
			return
		# Peel an optional ``template<...>`` prefix so the same forward-decl /
		# specialization filters apply whether or not it carries one.
		_, after_tpl = _split_template_prefix(stripped)
		# Forward declarations: `class Foo;` or `template<...> class Foo;`.
		# Also accept `class Foo<...>;` style explicit instantiations of a
		# specialization — the ``<...>`` is stripped first.
		fwd = _strip_trailing_template_args(after_tpl)
		if re.match(r"^(class|struct)\s+[A-Za-z_]\w*$", fwd):
			return
		# Explicit template instantiations / declarations. Not documentable.
		if re.match(r"^extern\s+template\b", stripped):
			return
		if re.match(r"^template\s+(class|struct)\b", stripped):
			return
		# `using` / `typedef` / `friend` (upstream filters these for class
		# scope; handle the namespace case too).
		if re.match(r"^(using|typedef|friend)\b", stripped):
			return
		# Out-of-line definitions of class members (e.g. `void Foo::Bar() {}`
		# or `const int Foo<T>::kVal = 0;`) should not appear as standalone
		# free functions/fields — the in-class declaration is the canonical
		# entry. Detect by a '::' that sits to the left of the first
		# top-level '(' (function head), or anywhere at all for fields.
		paren_i = _find_top_level_paren(stripped)
		head = stripped[:paren_i] if paren_i != -1 else stripped
		if _contains_top_level_scope_resolution(head):
			return
		# Free function: has top-level parentheses (outside <...>)
		if paren_i != -1:
			decl = self._match_function(stripped, line_no, None, doc, free_function=True)
			if decl is not None:
				self.raw_decls.append(decl)
			return
		# Variable / constant at namespace scope — not documented here.
		return

	def _match_member_decl(
		self,
		stmt: str,
		line_no: int,
		ctx: _ClassCtx,
		doc: Optional[DocBlock],
	) -> Optional[RawDecl]:
		# Normalize whitespace and strip leading decorations so classification
		# isn't confused by macro-argument parens.
		stripped = _strip_prefix_decorations(re.sub(r"\s+", " ", stmt).strip())
		# Skip operators and destructors — they are not shown in the docs.
		if re.search(r"\boperator\b", stripped):
			return None
		if re.search(r"(?:^|\s)~[A-Za-z_]\w*\s*\(", stripped):
			return None
		paren_i = _find_top_level_paren(stripped)
		eq_i = stripped.find("=")
		if paren_i != -1 and (eq_i == -1 or paren_i < eq_i):
			return self._match_function(stripped, line_no, ctx, doc, free_function=False)
		return self._match_field(stripped, line_no, ctx, doc)

	def _match_function(
		self,
		stmt: str,
		line_no: int,
		ctx: Optional[_ClassCtx],
		doc: Optional[DocBlock],
		free_function: bool,
	) -> Optional[RawDecl]:
		# Remove any `{ ... }` tail (inlined body) for matching.
		stmt = re.sub(r"\{[^{}]*\}\s*$", "", stmt).strip()
		# Extract template params
		template_params = None
		m_tpl = re.match(r"^template\s*<(.+?)>\s*(.*)$", stmt, re.DOTALL)
		if m_tpl:
			template_params = m_tpl.group(1).strip()
			stmt = m_tpl.group(2).strip()
		# Strip prefix macros/keywords (B3D_EXPORT, inline, virtual, static, etc.)
		stmt_stripped, flags = _strip_prefix_with_flags(stmt)
		# Find outermost paren group
		p_start = stmt_stripped.find("(")
		if p_start == -1:
			return None
		p_end = _matching_paren(stmt_stripped, p_start)
		if p_end == -1:
			return None
		head = stmt_stripped[:p_start].strip()
		params_raw = stmt_stripped[p_start + 1 : p_end]
		tail = stmt_stripped[p_end + 1 :].strip()
		# `head` ends with `Name` (possibly preceded by return type)
		m_name = re.search(r"([A-Za-z_~]\w*)\s*$", head)
		if not m_name:
			return None
		name = m_name.group(1)
		return_type = head[: m_name.start()].strip() or None
		# Return type None is fine for constructors/destructors.
		is_const = bool(re.search(r"\bconst\b", tail))
		# Build canonical signature
		params_clean = _clean_param_list(params_raw)
		sig_parts = []
		if template_params:
			sig_parts.append(f"template<{template_params}>")
		if flags.get("static"):
			sig_parts.append("static")
		if flags.get("virtual"):
			sig_parts.append("virtual")
		if flags.get("constexpr"):
			sig_parts.append("constexpr")
		if return_type:
			sig_parts.append(return_type)
		sig_parts.append(f"{name}({params_clean})")
		if is_const:
			sig_parts.append("const")
		if "override" in tail:
			sig_parts.append("override")
		signature = " ".join(sig_parts).replace("  ", " ").strip()
		qname = (
			self._qualify(name)
			if free_function
			else f"{ctx.qualified_name}::{name}"
		)
		visibility = "public" if free_function else ctx.visibility
		if ctx is not None and ctx.in_internal_name_block:
			visibility = "internal"
		return RawDecl(
			kind="function" if free_function else "method",
			name=name,
			qualified_name=qname,
			signature=signature,
			template_params=template_params,
			return_type=return_type,
			param_list=_split_params(params_raw),
			visibility=visibility,
			is_internal_name_block=(ctx.in_internal_name_block if ctx else False),
			is_static=bool(flags.get("static")),
			is_virtual=bool(flags.get("virtual")),
			is_const=is_const,
			parent_class_qname=(ctx.qualified_name if ctx else None),
			namespace=self._current_namespace(),
			group_stack=self._group_snapshot(),
			doc=doc or DocBlock(),
			location=SourceLoc(self.file, line_no),
		)

	def _match_field(
		self,
		stmt: str,
		line_no: int,
		ctx: _ClassCtx,
		doc: Optional[DocBlock],
	) -> Optional[RawDecl]:
		stmt_stripped, flags = _strip_prefix_with_flags(stmt)
		# Strip `{ ... }` aggregate initializers.
		stmt_stripped = re.sub(r"\{[^{}]*\}", "", stmt_stripped).strip()
		# Split off default value after '='
		default_value = None
		if "=" in stmt_stripped:
			idx = stmt_stripped.find("=")
			default_value = stmt_stripped[idx + 1 :].strip()
			stmt_stripped = stmt_stripped[:idx].strip()
		# Expect "Type Name" or "Type Name[...]"
		m = re.match(r"^(.*?)(?:\s)([A-Za-z_]\w*)\s*(?:\[[^\]]*\])?\s*$", stmt_stripped)
		if not m:
			return None
		return_type = m.group(1).strip()
		name = m.group(2)
		# Reject things that look like methods missed above
		if "(" in return_type:
			return None
		# Reject common non-field keywords
		if return_type in {"struct", "class", "enum", "return", "if", "while", "for"}:
			return None
		signature = f"{return_type} {name}" + (f" = {default_value}" if default_value else "")
		visibility = ctx.visibility
		if ctx.in_internal_name_block:
			visibility = "internal"
		return RawDecl(
			kind="field",
			name=name,
			qualified_name=f"{ctx.qualified_name}::{name}",
			signature=signature,
			return_type=return_type,
			default_value=default_value,
			visibility=visibility,
			is_internal_name_block=ctx.in_internal_name_block,
			is_static=bool(flags.get("static")),
			parent_class_qname=ctx.qualified_name,
			namespace=self._current_namespace(),
			group_stack=self._group_snapshot(),
			doc=doc or DocBlock(),
			location=SourceLoc(self.file, line_no),
		)


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------


def _find_top_level_paren(s: str) -> int:
	"""Return the index of the first '(' that is not inside angle brackets."""
	angle_depth = 0
	i = 0
	n = len(s)
	while i < n:
		c = s[i]
		if c == "<":
			angle_depth += 1
		elif c == ">":
			if angle_depth > 0:
				angle_depth -= 1
		elif c == "(" and angle_depth == 0:
			return i
		i += 1
	return -1


def _contains_top_level_scope_resolution(s: str) -> bool:
	"""True if `s` contains a `::` outside of any angle-bracket group. Used to
	detect out-of-line member definitions at namespace scope."""
	angle = 0
	i = 0
	n = len(s)
	while i < n - 1:
		c = s[i]
		if c == "<":
			angle += 1
		elif c == ">" and angle > 0:
			angle -= 1
		elif c == ":" and s[i + 1] == ":" and angle == 0:
			return True
		i += 1
	return False


def _find_top_level_colon(s: str) -> int:
	"""Return index of the first ':' that is not inside <>, (), [], or '::'."""
	angle = 0
	paren = 0
	bracket = 0
	i = 0
	n = len(s)
	while i < n:
		c = s[i]
		if c == "<":
			angle += 1
		elif c == ">":
			if angle > 0:
				angle -= 1
		elif c == "(":
			paren += 1
		elif c == ")":
			paren -= 1
		elif c == "[":
			bracket += 1
		elif c == "]":
			bracket -= 1
		elif c == ":" and angle == 0 and paren == 0 and bracket == 0:
			if i + 1 < n and s[i + 1] == ":":
				i += 2
				continue
			if i > 0 and s[i - 1] == ":":
				i += 1
				continue
			return i
		i += 1
	return -1


def _matching_paren(s: str, start: int) -> int:
	depth = 0
	i = start
	n = len(s)
	while i < n:
		c = s[i]
		if c == "(":
			depth += 1
		elif c == ")":
			depth -= 1
			if depth == 0:
				return i
		i += 1
	return -1


def _split_top_level(s: str, sep: str) -> list[str]:
	"""Split `s` on `sep` at bracket-depth zero."""
	depth = 0
	parts: list[str] = []
	buf: list[str] = []
	i = 0
	n = len(s)
	while i < n:
		c = s[i]
		if c in "({[<":
			depth += 1
			buf.append(c)
		elif c in ")}]>":
			depth -= 1
			buf.append(c)
		elif c == sep and depth == 0:
			parts.append("".join(buf))
			buf = []
		else:
			buf.append(c)
		i += 1
	if buf:
		parts.append("".join(buf))
	return parts


def _split_params(params_raw: str) -> list[tuple[str, str]]:
	out: list[tuple[str, str]] = []
	for part in _split_top_level(params_raw, ","):
		part = part.strip()
		if not part:
			continue
		# Strip default value
		if "=" in part:
			part = part[: part.find("=")].strip()
		# Last identifier is the name.
		m = re.search(r"([A-Za-z_]\w*)\s*$", part)
		if not m:
			out.append((part, ""))
			continue
		name = m.group(1)
		typ = part[: m.start()].strip()
		out.append((typ, name))
	return out


def _clean_param_list(params_raw: str) -> str:
	parts = _split_params(params_raw)
	rendered = []
	for typ, name in parts:
		if typ and name:
			rendered.append(f"{typ} {name}")
		elif typ:
			rendered.append(typ)
	return ", ".join(rendered)


_EXPORT_MACRO_RE = re.compile(r"^B3D_[A-Z0-9_]*EXPORT\b")


def _split_template_prefix(s: str) -> tuple[Optional[str], str]:
	"""If ``s`` begins with a balanced ``template<...>`` prefix, return
	``(args, rest)`` where args is the contents between the angle brackets and
	rest is the remainder of the string. Otherwise return ``(None, s)``.
	Handles nested angle brackets that the simple ``[^>]*`` regex cannot."""
	stripped = s.lstrip()
	if not stripped.startswith("template"):
		return None, s
	rest = stripped[len("template") :].lstrip()
	if not rest.startswith("<"):
		return None, s
	depth = 0
	for i, c in enumerate(rest):
		if c == "<":
			depth += 1
		elif c == ">":
			depth -= 1
			if depth == 0:
				return rest[1:i], rest[i + 1 :].lstrip()
	return None, s


def _strip_trailing_template_args(s: str) -> str:
	"""If ``s`` ends with a balanced ``<...>`` group, strip it. Used to peel
	the explicit-argument list off a partial-specialization class header so
	the underlying class name can be extracted."""
	s = s.rstrip()
	if not s.endswith(">"):
		return s
	depth = 0
	i = len(s) - 1
	while i >= 0:
		c = s[i]
		if c == ">":
			depth += 1
		elif c == "<":
			depth -= 1
			if depth == 0:
				return s[:i].rstrip()
		i -= 1
	return s


def _strip_prefix_decorations(s: str) -> str:
	"""Strip leading B3D_EXPORT, B3D_SCRIPT_EXPORT(...), [[...]] and similar."""
	changed = True
	while changed:
		changed = False
		s = s.lstrip()
		for word in DECL_PREFIX_SKIP_WORDS:
			if s.startswith(word) and (len(s) == len(word) or not (s[len(word)].isalnum() or s[len(word)] == "_")):
				s = s[len(word) :].lstrip()
				changed = True
				break
		if changed:
			continue
		# Any B3D_*_EXPORT macro. May or may not be followed by an argument list.
		m = _EXPORT_MACRO_RE.match(s)
		if m:
			s = s[m.end():].lstrip()
			if s.startswith("("):
				p_end = _matching_paren(s, 0)
				if p_end != -1:
					s = s[p_end + 1:].lstrip()
			changed = True
			continue
		# Function-like macros: NAME(...)
		m = re.match(r"^([A-Za-z_]\w*)\s*\(", s)
		if m and m.group(1) in {
			"B3D_SCRIPT_EXPORT",
			"B3D_PARAMETERS_BLOCK_BEGIN",
			"B3D_FLAGS_OPERATORS",
			"B3D_FLAGS_OPERATORS_EXT",
		}:
			name = m.group(1)
			p_start = s.find("(")
			p_end = _matching_paren(s, p_start)
			if p_end != -1:
				s = s[p_end + 1 :].lstrip()
				changed = True
				continue
		# [[nodiscard]] [[deprecated]]
		m = re.match(r"^\[\[[^\]]*\]\]", s)
		if m:
			s = s[m.end() :].lstrip()
			changed = True
			continue
	return s


def _strip_prefix_with_flags(s: str) -> tuple[str, dict]:
	flags = {"static": False, "virtual": False, "constexpr": False, "inline": False}
	changed = True
	while changed:
		changed = False
		s = s.lstrip()
		m = re.match(r"^(static|virtual|constexpr|inline|explicit)\b", s)
		if m:
			flags[m.group(1)] = True if m.group(1) in flags else False
			if m.group(1) in flags:
				flags[m.group(1)] = True
			s = s[m.end() :].lstrip()
			changed = True
			continue
		s2 = _strip_prefix_decorations(s)
		if s2 != s:
			s = s2
			changed = True
	return s, flags


# ----------------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------------


def parse_file(path: Path, rel_path: str) -> tuple[list[RawDecl], list[GroupDecl]]:
	try:
		text = path.read_text(encoding="utf-8", errors="replace")
	except OSError as e:
		warn(f"could not read {rel_path}: {e}")
		return [], []
	parser = _FileParser(rel_path)
	for kind, content, line_no in _tokenize(text):
		if kind == "comment":
			parser.handle_comment(content, line_no)
			# A comment may also set @name Internal flag
			cleaned = _clean_comment(content)
			if re.search(r"@name\s+Internal\b", cleaned, re.IGNORECASE):
				if parser.class_stack:
					parser.class_stack[-1].in_internal_name_block = True
			elif re.search(r"@name\s+\S+", cleaned):
				if parser.class_stack:
					parser.class_stack[-1].in_internal_name_block = False
		else:
			parser.handle_code(content, line_no)
	return parser.raw_decls, parser.group_decls


def parse_files(
	paths: list[tuple[Path, str]],
) -> tuple[list[RawDecl], list[GroupDecl]]:
	all_decls: list[RawDecl] = []
	all_groups: list[GroupDecl] = []
	for path, rel in paths:
		try:
			decls, groups = parse_file(path, rel)
		except Exception as e:  # noqa: BLE001
			warn(f"parser error in {rel}: {e}")
			continue
		all_decls.extend(decls)
		all_groups.extend(groups)
	vlog(f"parsed {len(paths)} files, {len(all_decls)} decls, {len(all_groups)} group refs")
	return all_decls, all_groups
