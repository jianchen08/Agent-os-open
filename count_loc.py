import collections
import os

ROOT = r"D:\myproject\container_e17cc5927dfd"

# dirs to skip entirely (deps / build / venv / caches / tooling / runtime)
SKIP_DIRS = {
    ".git",
    "node_modules",
    "__pycache__",
    "target",
    "dist",
    "dist-electron",
    "build",
    ".next",
    ".vite",
    "coverage",
    ".workbuddy",
    ".idea",
    ".vs",
    "venv",
    ".venv",
    "out",
    # venvs (any flavor)
    ".venv-hindsight",
    "site-packages",
    ".tox",
    ".hypothesis",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    # tool / AI caches
    ".zcode",
    ".zcode_e2e",
    ".ai_workspaces",
    ".dbg",  # runtime artifacts
    "data",
    "logs",
    "reports",
    "hosts",
    "docker",
    "agent-research",
    "agent_os.egg-info",
}

SKIP_EXT = {
    "pyc",
    "pyo",
    "d",
    "rmeta",
    "rlib",
    "pdb",
    "exe",
    "dll",
    "so",
    "o",
    "obj",
    "a",
    "lib",
    "map",
    "wasm",
    "class",
    "jar",
    "png",
    "jpg",
    "jpeg",
    "gif",
    "ico",
    "bmp",
    "svg",
    "zip",
    "tar",
    "gz",
    "7z",
    "rar",
    "pdf",
    "ttf",
    "woff",
    "woff2",
    "eot",
    "db",
    "db-shm",
    "db-wal",
    "sqlite",
    "bin",
    "dat",
    "mp4",
    "mp3",
    "wav",
    "webm",
    "mov",
    "whl",
    "egg",
}

LANG = {
    "py": "Python",
    "rs": "Rust",
    "ts": "TypeScript",
    "tsx": "TSX (React)",
    "js": "JavaScript",
    "jsx": "JSX",
    "mjs": "JavaScript",
    "cjs": "JavaScript",
    "css": "CSS",
    "scss": "SCSS",
    "less": "Less",
    "html": "HTML",
    "htm": "HTML",
    "vue": "Vue",
    "go": "Go",
    "java": "Java",
    "cpp": "C++",
    "cc": "C++",
    "cxx": "C++",
    "c": "C",
    "h": "C/C++ Header",
    "hpp": "C++ Header",
    "sh": "Shell",
    "bash": "Shell",
    "bat": "Batch",
    "ps1": "PowerShell",
    "yaml": "YAML",
    "yml": "YAML",
    "toml": "TOML",
    "json": "JSON",
    "md": "Markdown",
    "txt": "Text",
    "rst": "reStructuredText",
    "sql": "SQL",
    "proto": "Proto",
    "graphql": "GraphQL",
    "lua": "Lua",
    "r": "R",
    "ipynb": "Jupyter",
}

CATEGORY = {
    "Python": "Source",
    "Rust": "Source",
    "TypeScript": "Source",
    "TSX (React)": "Source",
    "JavaScript": "Source",
    "JSX": "Source",
    "CSS": "Source",
    "SCSS": "Source",
    "Less": "Source",
    "HTML": "Source",
    "Vue": "Source",
    "Go": "Source",
    "Java": "Source",
    "C++": "Source",
    "C": "Source",
    "C/C++ Header": "Source",
    "C++ Header": "Source",
    "SQL": "Source",
    "Proto": "Proto/IDL",
    "GraphQL": "Proto/IDL",
    "PowerShell": "Scripts",
    "Shell": "Scripts",
    "Batch": "Scripts",
    "YAML": "Config",
    "TOML": "Config",
    "JSON": "Config/Data",
    "Markdown": "Docs",
    "Text": "Docs",
    "reStructuredText": "Docs",
    "Lua": "Source",
    "R": "Source",
    "Jupyter": "Docs",
}

stats: collections.defaultdict[str, list[int]] = collections.defaultdict(lambda: [0, 0])
total_files = 0
total_lines = 0

for dirpath, dirnames, filenames in os.walk(ROOT):
    dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
    for fn in filenames:
        ext = fn.rsplit(".", 1)[-1].lower() if "." in fn else ""
        if ext in SKIP_EXT:
            continue
        lang = LANG.get(ext)
        if lang is None:
            continue
        fp = os.path.join(dirpath, fn)
        try:
            with open(fp, "rb") as fh:
                data = fh.read()
        except Exception:
            continue
        lines = data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)
        stats[lang][0] += 1
        stats[lang][1] += lines
        total_files += 1
        total_lines += lines

out = []
out.append("=== Lines of Code by Language (deps/caches/build excluded) ===")
out.append(f"{'Language':<18}{'Files':>8}{'Lines':>12}")
out.append("-" * 40)
for lang in sorted(stats, key=lambda k: -stats[k][1]):
    files, lines = stats[lang]
    out.append(f"{lang:<18}{files:>8}{lines:>12}")

out.append("\n=== By Category ===")
cat: collections.defaultdict[str, list[int]] = collections.defaultdict(lambda: [0, 0])
for lang, (files, lines) in stats.items():
    c = CATEGORY.get(lang, "Other")
    cat[c][0] += files
    cat[c][1] += lines
out.append(f"{'Category':<18}{'Files':>8}{'Lines':>12}")
out.append("-" * 40)
for c in sorted(cat, key=lambda k: -cat[k][1]):
    files, lines = cat[c]
    out.append(f"{c:<18}{files:>8}{lines:>12}")

out.append("-" * 40)
out.append(f"{'TOTAL':<18}{total_files:>8}{total_lines:>12}")

with open(r"D:\myproject\container_e17cc5927dfd\count_loc_report.txt", "w", encoding="utf-8") as rf:
    rf.write("\n".join(out) + "\n")
