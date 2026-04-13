# BansheeDocGenerator

Static HTML documentation generator for the Banshee Engine. Parses Doxygen-style
Javadoc comments in C++ headers and Markdown manuals, and emits a self-contained
website into `Framework/Documentation/GeneratedSite/`.

## Usage

From the repository root:

```
python -m bansheedocgenerator build
```

Or via launcher:

```
Framework/Tools/BansheeDocGenerator/bansheedocgenerator.cmd build
```

### Flags

- `--source PATH [PATH ...]` — directories to scan for C++ headers
  (default: `Framework/Source` `Source`)
- `--manuals PATH` — directory of Markdown manuals
  (default: `Framework/Documentation/Manuals/docs`)
- `--output PATH` — output directory
  (default: `Framework/Documentation/GeneratedSite`)
- `--clean` — wipe output directory before building
- `--serve [PORT]` — serve the result via `http.server` after building
- `--verbose` — print per-phase timing and warnings

## Dependencies

```
pip install jinja2 markdown-it-py mdit-py-plugins pygments
```
