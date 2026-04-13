"""Small shared utilities: logging, path helpers, name sanitization."""

from __future__ import annotations

import re
import sys
from pathlib import Path

_VERBOSE = False


def set_verbose(v: bool) -> None:
	global _VERBOSE
	_VERBOSE = v


def log(msg: str) -> None:
	print(msg, file=sys.stderr)


def vlog(msg: str) -> None:
	if _VERBOSE:
		print(msg, file=sys.stderr)


def warn(msg: str) -> None:
	print(f"warning: {msg}", file=sys.stderr)


def to_posix(path: Path) -> str:
	return str(path).replace("\\", "/")


def repo_relative(path: Path, root: Path) -> str:
	try:
		return to_posix(path.resolve().relative_to(root.resolve()))
	except ValueError:
		return to_posix(path)


_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]")


def safe_filename(name: str) -> str:
	"""Convert a qualified C++ name into a filesystem-safe filename component."""
	return _SAFE_FILENAME_RE.sub("_", name.replace("::", "__"))


_ANCHOR_SAFE_RE = re.compile(r"[^A-Za-z0-9_-]")


def safe_anchor(name: str) -> str:
	"""Anchor-safe version of an identifier."""
	return _ANCHOR_SAFE_RE.sub("-", name)


def relative_link(from_url: str, to_url: str) -> str:
	"""Compute a site-relative link from one page to another.

	Both inputs are site-relative posix paths. Trailing #fragments on `to_url`
	are preserved.
	"""
	if to_url.startswith(("http://", "https://", "mailto:")):
		return to_url
	fragment = ""
	if "#" in to_url:
		to_url, fragment = to_url.split("#", 1)
		fragment = "#" + fragment
	from_parts = from_url.split("/")[:-1]
	to_parts = to_url.split("/")
	# drop common prefix
	i = 0
	while i < len(from_parts) and i < len(to_parts) - 1 and from_parts[i] == to_parts[i]:
		i += 1
	up = ["..",] * (len(from_parts) - i)
	down = to_parts[i:]
	rel = "/".join(up + down) if (up or down) else to_parts[-1]
	return rel + fragment
