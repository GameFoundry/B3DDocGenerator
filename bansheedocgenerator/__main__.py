"""CLI entry point for BansheeDocGenerator."""

from __future__ import annotations

import argparse
import bisect
import sys
import time
from pathlib import Path

from . import __version__
from .asset_emitter import copy_manual_images, emit_assets
from .config import (
	DEFAULT_DOCGEN_JSON,
	DEFAULT_MANUALS_DIR,
	DEFAULT_OUTPUT_DIR,
	DEFAULT_SOURCE_DIRS,
	REPO_ROOT,
)
from .group_resolver import resolve_groups
from .ir_builder import build_ir
from .json_parser import parse_json
from .manual_renderer import render_manuals
from .manual_scanner import scan_manuals
from .api_renderer import render_site
from .model import RawDecl, Site
from .search_indexer import write_search_index
from .source_scanner import scan_sources
from .util import log, repo_relative, set_verbose, vlog


def _collect_headers(source_dirs: list[Path]) -> list[tuple[Path, str]]:
	out: list[tuple[Path, str]] = []
	for src in source_dirs:
		if not src.exists():
			log(f"warning: source dir not found: {src}")
			continue
		for p in src.rglob("*.h"):
			rel = repo_relative(p, REPO_ROOT)
			# Skip third-party
			if "/ThirdParty/" in rel or "/Dependencies/" in rel:
				continue
			out.append((p, rel))
	return out


def _attach_scope_info(
	raw_decls: list[RawDecl],
	internal_ranges_by_file: dict[str, list[tuple[int, int]]],
	group_spans_by_file: dict[str, list[tuple[str, int, int]]],
) -> None:
	"""Walk every RawDecl and attach its group_stack / is_internal_name_block
	by looking its source location up in the per-file range maps built by
	``source_scanner``."""
	# Pre-sort the group span lists by start-line so we can bisect.
	sorted_group_spans: dict[str, tuple[list[int], list[tuple[str, int, int]]]] = {}
	for key, spans in group_spans_by_file.items():
		spans_sorted = sorted(spans, key=lambda s: s[1])
		starts = [s[1] for s in spans_sorted]
		sorted_group_spans[key] = (starts, spans_sorted)

	for raw in raw_decls:
		if raw.location is None:
			continue
		file_key = raw.location.file
		line = raw.location.line

		spans_entry = sorted_group_spans.get(file_key)
		if spans_entry is not None:
			starts, spans_sorted = spans_entry
			idx = bisect.bisect_right(starts, line)
			stack: list[str] = []
			for span_idx in range(idx):
				name, start, end = spans_sorted[span_idx]
				if start <= line <= end:
					stack.append(name)
			if stack:
				raw.group_stack = stack

		ranges = internal_ranges_by_file.get(file_key)
		if ranges and raw.kind in ("method", "field"):
			for start, end in ranges:
				if start <= line <= end:
					raw.is_internal_name_block = True
					break


def build_command(args: argparse.Namespace) -> int:
	t0 = time.time()
	set_verbose(bool(args.verbose))
	output_dir = Path(args.output).resolve()
	if args.clean and output_dir.exists():
		import shutil
		shutil.rmtree(output_dir)
	output_dir.mkdir(parents=True, exist_ok=True)

	json_path = Path(args.json).resolve()
	source_dirs = [Path(p).resolve() for p in args.source]
	manuals_dir = Path(args.manuals).resolve()

	log(f"BansheeDocGenerator {__version__}")
	log(f"  docgen json: {json_path}")
	log(f"  source dirs: {[str(p) for p in source_dirs]}")
	log(f"  manuals:     {manuals_dir}")
	log(f"  output:      {output_dir}")

	if not json_path.exists():
		log(f"error: docgen JSON not found at {json_path}")
		log("       run BansheeCodeGenerator to produce it before building docs.")
		return 2

	# Phase 1a: load declarations from the BansheeCodeGenerator JSON dump.
	log("phase 1a: loading docgen JSON")
	t = time.time()
	raw_decls = parse_json(json_path)
	log(f"  done ({time.time() - t:.1f}s): {len(raw_decls)} decls")

	# Phase 1b: scan original headers for group markers and @name Internal
	# blocks (neither survives into the JSON dump).
	headers = _collect_headers(source_dirs)
	log(f"phase 1b: scanning {len(headers)} headers for group/internal markers")
	t = time.time()
	group_decls, internal_ranges, group_spans = scan_sources(headers)
	log(
		f"  done ({time.time() - t:.1f}s): {len(group_decls)} group refs, "
		f"{sum(len(v) for v in internal_ranges.values())} internal ranges across "
		f"{len(internal_ranges)} files"
	)

	# Cross-reference: attach scope info onto the raw decls.
	_attach_scope_info(raw_decls, internal_ranges, group_spans)

	# Phase 2: resolve group taxonomy
	log("phase 2: resolving groups")
	groups, root_group_order = resolve_groups(group_decls)

	# Phase 3: scan manuals
	log("phase 3: scanning manuals")
	manuals, manual_order, manual_tree = scan_manuals(manuals_dir)
	log(f"  found {len(manuals)} manuals")

	# Phase 4: build IR
	log("phase 4: building IR")
	site = Site()
	site.root_group_order = root_group_order
	site.manuals = manuals
	site.manual_tree = manual_tree
	site.root_manual_order = manual_order
	build_ir(raw_decls, groups, site)
	log(f"  {len(site.classes)} classes, {len(site.enums)} enums, {len(site.functions)} free functions")

	# Phase 5a: render manuals (first, so headings are populated before we also use them)
	log("phase 5a: rendering manuals")
	render_manuals(site)

	# Phase 5b: render API + manual pages
	log("phase 5b: rendering HTML pages")
	template_dir = Path(__file__).parent / "templates"
	render_site(site, template_dir, output_dir)

	# Phase 6: copy static assets (wipes output/static — must run before search index)
	log("phase 6: emitting static assets")
	emit_assets(template_dir, output_dir)
	copy_manual_images(manuals_dir, output_dir)

	# Phase 5c: search index
	log("phase 5c: building search index")
	write_search_index(site, output_dir)

	log(f"done in {time.time() - t0:.1f}s -> {output_dir}")

	if args.serve:
		_serve(output_dir, args.serve_port)

	return 0


def _serve(output_dir: Path, port: int) -> None:
	import http.server
	import socketserver

	handler = http.server.SimpleHTTPRequestHandler
	import os
	os.chdir(str(output_dir))
	with socketserver.TCPServer(("127.0.0.1", port), handler) as httpd:
		log(f"serving at http://127.0.0.1:{port}/ (Ctrl+C to stop)")
		try:
			httpd.serve_forever()
		except KeyboardInterrupt:
			log("stopped.")


def main(argv: list[str] | None = None) -> int:
	ap = argparse.ArgumentParser(prog="bansheedocgenerator", description="Banshee Engine documentation generator")
	sub = ap.add_subparsers(dest="command")

	build_ap = sub.add_parser("build", help="Generate the documentation site.")
	build_ap.add_argument(
		"--json",
		default=str(DEFAULT_DOCGEN_JSON),
		help="BansheeCodeGenerator docgen JSON file to ingest.",
	)
	build_ap.add_argument(
		"--source",
		nargs="+",
		default=[str(p) for p in DEFAULT_SOURCE_DIRS],
		help="One or more source directories to scan for @defgroup / @addtogroup / @name Internal markers.",
	)
	build_ap.add_argument(
		"--manuals",
		default=str(DEFAULT_MANUALS_DIR),
		help="Directory of Markdown manuals.",
	)
	build_ap.add_argument(
		"--output",
		default=str(DEFAULT_OUTPUT_DIR),
		help="Output directory for the generated site.",
	)
	build_ap.add_argument("--clean", action="store_true", help="Wipe output directory before building.")
	build_ap.add_argument(
		"--serve",
		nargs="?",
		const=True,
		default=False,
		help="Serve the output directory via http.server after building.",
	)
	build_ap.add_argument("--serve-port", type=int, default=8080, help="Port for --serve (default 8080).")
	build_ap.add_argument("--verbose", action="store_true", help="Print per-phase timing and warnings.")

	args = ap.parse_args(argv)
	if args.command == "build":
		return build_command(args)
	ap.print_help()
	return 1


if __name__ == "__main__":
	sys.exit(main())
