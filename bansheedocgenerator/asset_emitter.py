"""Copies static assets (CSS, JS, vendor libs, images) into the output dir
and vendors MiniSearch from a bundled stub.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from .util import vlog


# A very small vendored MiniSearch stub. In a real deploy, replace this with
# the actual MiniSearch UMD build. We inline a minimal stand-in so the site
# still renders without a network trip; the real library can be dropped into
# static/vendor/minisearch.min.js and the fallback will not be used.

_MINISEARCH_FALLBACK = r"""// Fallback MiniSearch shim — substring search only.
// Drop the real library from https://github.com/lucaong/minisearch into this file for real search.
(function(global) {
	function MiniSearch(opts) {
		this.opts = opts || {};
		this.fields = (opts && opts.fields) || ["name", "qname", "brief"];
		this.docs = [];
	}
	MiniSearch.prototype.addAll = function(items) {
		for (var i = 0; i < items.length; i++) this.docs.push(items[i]);
	};
	MiniSearch.prototype.search = function(query) {
		var q = (query || "").toLowerCase();
		if (!q) return [];
		var results = [];
		for (var i = 0; i < this.docs.length; i++) {
			var d = this.docs[i];
			var score = 0;
			for (var f = 0; f < this.fields.length; f++) {
				var field = this.fields[f];
				var v = (d[field] || "").toLowerCase();
				if (!v) continue;
				var idx = v.indexOf(q);
				if (idx !== -1) {
					score += (v === q ? 100 : (idx === 0 ? 10 : 1));
					if (field === "name") score *= 2;
				}
			}
			if (score > 0) results.push({ d: d, score: score });
		}
		results.sort(function(a, b) { return b.score - a.score; });
		return results.map(function(r) { return r.d; });
	};
	global.MiniSearch = MiniSearch;
})(typeof window !== "undefined" ? window : this);
"""


def emit_assets(template_dir: Path, output_dir: Path) -> None:
	static_src = template_dir.parent / "static"
	static_dst = output_dir / "static"
	if static_dst.exists():
		shutil.rmtree(static_dst)
	shutil.copytree(static_src, static_dst)

	# If a real MiniSearch build isn't present, drop the shim in place.
	ms = static_dst / "vendor" / "minisearch.min.js"
	if not ms.exists() or ms.stat().st_size < 100:
		ms.parent.mkdir(parents=True, exist_ok=True)
		ms.write_text(_MINISEARCH_FALLBACK, encoding="utf-8")


def copy_manual_images(manuals_root: Path, output_dir: Path) -> None:
	"""Copy the `Images/` directory from the manual source to output."""
	src = manuals_root / "Images"
	if not src.exists():
		return
	dst = output_dir / "manuals" / "images"
	if dst.exists():
		shutil.rmtree(dst)
	shutil.copytree(src, dst)
	vlog(f"copied {len(list(src.iterdir()))} manual images")
