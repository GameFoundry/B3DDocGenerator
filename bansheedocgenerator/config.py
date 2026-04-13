"""Static configuration constants and defaults for BansheeDocGenerator."""

from __future__ import annotations

from pathlib import Path

# Repository layout resolution. The tool lives under Framework/Tools/BansheeDocGenerator/bansheedocgenerator/,
# so the repo root is four parents up (bansheedocgenerator -> BansheeDocGenerator -> Tools -> Framework -> repo).
THIS_FILE = Path(__file__).resolve()
REPO_ROOT = THIS_FILE.parents[4]

DEFAULT_SOURCE_DIRS = [
	REPO_ROOT / "Framework" / "Source",
	REPO_ROOT / "Source",
]

DEFAULT_MANUALS_DIR = REPO_ROOT / "Framework" / "Documentation" / "Manuals" / "docs"

DEFAULT_OUTPUT_DIR = REPO_ROOT / "Framework" / "Documentation" / "GeneratedSite"

# Canonical group taxonomy source (every @defgroup is expected to live here).
GROUP_TAXONOMY_FILE = REPO_ROOT / "Framework" / "Source" / "Engine" / "Core" / "B3DPrerequisites.h"

# Prefixes that may appear between a /** */ comment and the declaration it documents.
# The parser skips these so it can still find the declaration keyword.
DECL_PREFIX_SKIP_WORDS = {
	"B3D_EXPORT",
	"B3D_CORE_EXPORT",
	"B3D_UTILITY_EXPORT",
	"B3D_EDITOR_EXPORT",
	"B3D_EDITOR_SCRIPT_INTEROP_EXPORT",
	"B3D_FRAMEWORK_TESTS_EXPORT",
	"B3D_MONO_EXPORT",
	"B3D_PLUGIN_EXPORT",
	"B3D_SCR_BE_EXPORT",
	"B3D_SCRIPT_INTEROP_EXPORT",
	"inline",
	"static",
	"constexpr",
	"consteval",
	"virtual",
	"explicit",
	"friend",
	"typename",
}

# Macros that look like function calls (take arguments in parentheses) and must
# be bracket-balanced when skipping.
DECL_PREFIX_SKIP_MACROS = {
	"B3D_SCRIPT_EXPORT",
	"B3D_PARAMETERS_BLOCK_BEGIN",
	"[[nodiscard]]",
	"[[deprecated]]",
}

# Namespaces that we actively document. Used when constructing qualified names
# and when resolving @b3d:: references in manuals.
PRIMARY_NAMESPACE = "b3d"

# Substring that marks a group as "internal" documentation.
INTERNAL_MARKER = "internal"
