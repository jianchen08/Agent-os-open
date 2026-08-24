import os, re, collections

ROOT = r"D:\myproject\container_e17cc5927dfd"

SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", "target", "dist",
    "dist-electron", "build", ".next", ".vite", "coverage",
    ".workbuddy", ".idea", ".vs", "venv", ".venv", "out",
    ".venv-hindsight", "site-packages", ".tox", ".hypothesis",
    ".pytest_cache", ".ruff_cache", ".mypy_cache",
    ".zcode", ".zcode_e2e", ".ai_workspaces", ".dbg",
    "data", "logs", "reports", "hosts", "docker",
    "agent-research", "agent_os.egg-info",
}
SKIP_EXT = {
    "pyc","pyo","d","rmeta","rlib","pdb","exe","dll","so","o","obj","a","lib",
    "map","wasm","class","jar","png","jpg","jpeg","gif","ico","bmp","svg",
    "zip","tar","gz","7z","rar","pdf","ttf","woff","woff2","eot",
    "db","db-shm","db-wal","sqlite","bin","dat","mp4","mp3","wav","webm","mov","whl","egg",
}
LANG = {
    "py":"Python","rs":"Rust","ts":"TypeScript","tsx":"TSX",
    "js":"JavaScript","jsx":"JSX","mjs":"JavaScript","cjs":"JavaScript",
    "css":"CSS","scss":"SCSS","less":"Less","html":"HTML","htm":"HTML","vue":"Vue",
    "go":"Go","java":"Java","cpp":"C++","cc":"C++","cxx":"C++","c":"C",
    "h":"C/C++ Header","hpp":"C++ Header","sh":"Shell","bash":"Shell",
    "bat":"Batch","ps1":"PowerShell","yaml":"YAML","yml":"YAML","toml":"TOML",
    "json":"JSON","md":"Markdown","txt":"Text","rst":"reStructuredText",
    "sql":"SQL","proto":"Proto","graphql":"GraphQL","lua":"Lua","r":"R","ipynb":"Jupyter",
}

# module attribution
def module_of(rel):
    parts = rel.replace("\\","/").split("/")
    top = parts[0]
    if top == "frontend": return "frontend"
    if top == "plugins": return "plugins"
    if top == "kernel": return "kernel"
    if top == "tests": return "plugins"   # python pytest suite tests the plugins
    return "other"

def is_test(rel, name):
    p = rel.replace("\\","/").lower()
    nl = name.lower()
    if nl.startswith("test_") and nl.endswith(".py"): return True
    if nl.endswith("_test.py"): return True
    if "/tests/" in p: return True
    if re.search(r"\.(test|spec)\.(ts|tsx|js|jsx)$", nl): return True
    if "/__tests__/" in p: return True
    return False

# record[module][kind][lang] = [files, lines]
record = collections.defaultdict(lambda: collections.defaultdict(lambda: collections.defaultdict(lambda: [0,0])))

for dirpath, dirnames, filenames in os.walk(ROOT):
    dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
    for fn in filenames:
        ext = fn.rsplit(".",1)[-1].lower() if "." in fn else ""
        if ext in SKIP_EXT: continue
        lang = LANG.get(ext)
        if lang is None: continue
        rel = os.path.relpath(os.path.join(dirpath,fn), ROOT)
        mod = module_of(rel)
        kind = "test" if is_test(rel, fn) else "business"
        try:
            with open(os.path.join(dirpath,fn),"rb") as f:
                data=f.read()
        except Exception:
            continue
        lines = data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)
        record[mod][kind][lang][0] += 1
        record[mod][kind][lang][1] += lines

def tot(d):
    f=l=0
    for lang,v in d.items():
        f+=v[0]; l+=v[1]
    return f,l

out=[]
modules=["frontend","plugins","kernel","other"]
out.append(f"{'Module':<12}{'Kind':<10}{'Files':>8}{'Lines':>12}")
out.append("-"*42)
for mod in modules:
    if mod not in record: continue
    for kind in ("business","test"):
        if kind not in record[mod]: continue
        f,l=tot(record[mod][kind])
        out.append(f"{mod:<12}{kind:<10}{f:>8}{l:>12}")
    bf,bl=tot(record[mod].get("business",{}))
    tf,tl=tot(record[mod].get("test",{}))
    if bf+bl>0:
        ratio = (tl/(bl+tl)*100) if (bl+tl)>0 else 0
        out.append(f"{'':<12}{'(test%)':<10}{'':>8}{ratio:>11.1f}%")
    out.append("")

# detailed language split for the 3 main modules
out.append("")
out.append("=== Detailed: business vs test by language (frontend/plugins/kernel) ===")
for mod in ["frontend","plugins","kernel"]:
    out.append(f"\n--- {mod} ---")
    out.append(f"{'Language':<14}{'BizFiles':>9}{'BizLines':>10}{'TestFiles':>10}{'TestLines':>10}")
    out.append("-"*53)
    langs = sorted(set(record[mod].get("business",{}).keys()) | set(record[mod].get("test",{}).keys()))
    for lang in sorted(langs, key=lambda k: -(record[mod].get("business",{}).get(k,[0,0])[1]+record[mod].get("test",{}).get(k,[0,0])[1])):
        bf,bl = record[mod].get("business",{}).get(lang,[0,0])
        tf,tl = record[mod].get("test",{}).get(lang,[0,0])
        out.append(f"{lang:<14}{bf:>9}{bl:>10}{tf:>10}{tl:>10}")

with open(r"D:\myproject\container_e17cc5927dfd\count_loc_detail_report.txt","w",encoding="utf-8") as f:
    f.write("\n".join(out)+"\n")
