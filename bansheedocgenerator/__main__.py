"""CLI entry point for BansheeDocGenerator."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from . import __version__
from .asset_emitter import copy_manual_images, emit_assets
from .config import (
	DEFAULT_MANUALS_DIR,
	DEFAULT_OUTPUT_DIR,
	DEFAULT_SOURCE_DIRS,
	REPO_ROOT,
)
from .cpp_parser import parse_files
from .group_resolver import resolve_groups
from .ir_builder import build_ir
from .manual_renderer import render_manuals
from .manual_scanner import scan_manuals
from .api_renderer import render_site
from .model import Site
from .search_indexer import write_search_index
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


def build_command(args: argparse.Namespace) -> int:
	t0 = time.time()
	set_verbose(bool(args.verbose))
	output_dir = Path(args.output).resolve()
	if args.clean and output_dir.exists():
		import shutil
		shutil.rmtree(output_dir)
	output_dir.mkdir(parents=True, exist_ok=True)

	source_dirs = [Path(p).resolve() for p in args.source]
	manuals_dir = Path(args.manuals).resolve()

	log(f"BansheeDocGenerator {__version__}")
	log(f"  source dirs: {[str(p) for p in source_dirs]}")
	log(f"  manuals:     {manuals_dir}")
	log(f"  output:      {output_dir}")

	# Phase 1: collect and parse headers
	headers = _collect_headers(source_dirs)
	log(f"phase 1: parsing {len(headers)} headers")
	t = time.time()
	raw_decls, group_decls = parse_files(headers)
	log(f"  done ({time.time() - t:.1f}s): {len(raw_decls)} decls, {len(group_decls)} group refs")

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
		"--source",
		nargs="+",
		default=[str(p) for p in DEFAULT_SOURCE_DIRS],
		help="One or more source directories to scan for .h headers.",
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
