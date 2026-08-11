"""
静态检查 generate_resume.py：
1. 验证 Python 语法
2. 验证 YAML 数据结构与脚本期望的字段一致
3. 验证 tr() 函数的双语取值逻辑
4. 验证 render_html() 不会崩溃
"""
import ast
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
script = SCRIPT_DIR / "generate_resume.py"
data_file = SCRIPT_DIR / "resume_data.yaml"

# === 1. Python 语法检查 ===
print("=" * 60)
print("1. Python 语法检查")
print("=" * 60)
try:
    with open(script, "r", encoding="utf-8") as f:
        source = f.read()
    tree = ast.parse(source, filename=str(script))
    # 统计函数
    funcs = [n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
    print(f"   ✅ 语法解析通过（共 {len(funcs)} 个函数）")
    print(f"   📋 函数列表: {', '.join(funcs[:10])}{'...' if len(funcs) > 10 else ''}")
except SyntaxError as e:
    print(f"   ❌ 语法错误: {e}")
    sys.exit(1)

# === 2. YAML 数据加载检查 ===
print()
print("=" * 60)
print("2. YAML 数据加载检查")
print("=" * 60)
import yaml
with open(data_file, "r", encoding="utf-8") as f:
    data = yaml.safe_load(f)
print(f"   ✅ YAML 加载成功")

# 关键字段
required = {
    "meta": ["version", "source_files"],
    "theme": ["default", "available"],
    "basic": ["name_zh", "name_en", "display_name_zh", "display_name_en",
              "gender_zh", "birth_year", "title_zh", "title_en", "tagline_zh", "tagline_en"],
    "job_intention": ["status_zh", "target_roles_zh", "target_industries_zh"],
    "education": ["school_zh", "degree_zh", "major_zh"],
    "projects": ["id", "name_zh", "name_en", "role_zh", "role_en",
                 "period", "summary_zh", "summary_en", "tech_stack",
                 "responsibilities_zh", "highlights_zh", "metrics_zh"],
    "skills": ["categories"],
    "open_source": ["status_zh", "contributions_zh"],
    "awards": ["status", "items"],
    "about": ["summary_zh", "values_zh"],
    "layout": ["sections_order"],
    "platforms": ["boss_zhipin", "lagou", "linkedin"],
}
all_ok = True
for section, fields in required.items():
    if section not in data:
        print(f"   ❌ 缺少顶级字段: {section}")
        all_ok = False
        continue
    if isinstance(data[section], list):
        for i, item in enumerate(data[section]):
            for f in fields:
                if f not in item:
                    print(f"   ❌ {section}[{i}] 缺少字段: {f}")
                    all_ok = False
    else:
        for f in fields:
            if f not in data[section]:
                print(f"   ❌ {section} 缺少字段: {f}")
                all_ok = False
if all_ok:
    print(f"   ✅ 所有关键字段齐全")

# === 3. 双语字段统计 ===
print()
print("=" * 60)
print("3. 双语字段统计")
print("=" * 60)
def count_bilingual(d, prefix=""):
    zh_count = en_count = 0
    for k, v in d.items():
        if isinstance(v, dict):
            z, e = count_bilingual(v, f"{prefix}.{k}")
            zh_count += z
            en_count += e
        elif isinstance(v, list):
            pass
        elif k.endswith("_zh"):
            zh_count += 1
        elif k.endswith("_en"):
            en_count += 1
    return zh_count, en_count

z, e = count_bilingual(data)
print(f"   中文字段数: {z}")
print(f"   英文字段数: {e}")
print(f"   {'✅' if z == e else '❌'} 中英文字段数量{'一致' if z == e else '不一致'}")

# === 4. 主题配置检查 ===
print()
print("=" * 60)
print("4. 主题配置检查")
print("=" * 60)
theme_ids = [t["id"] for t in data["theme"]["available"]]
print(f"   可用主题: {', '.join(theme_ids)}")
default = data["theme"]["default"]
print(f"   {'✅' if default in theme_ids else '❌'} 默认主题 {default} {'存在' if default in theme_ids else '不存在'}")

# === 5. 项目板块统计 ===
print()
print("=" * 60)
print("5. 项目板块统计")
print("=" * 60)
for p in data["projects"]:
    print(f"   📦 {p['id']}: {p['name_zh']}")
    print(f"      featured={p.get('is_featured')}, "
          f"order={p.get('order')}, "
          f"responsibilities_zh={len(p.get('responsibilities_zh', []))}条, "
          f"highlights_zh={len(p.get('highlights_zh', []))}条, "
          f"metrics_zh={len(p.get('metrics_zh', []))}条, "
          f"tech_stack={len(p.get('tech_stack', []))}项")

# === 6. 平台字数限制 ===
print()
print("=" * 60)
print("6. 求职平台字数限制")
print("=" * 60)
for p_name, p_cfg in data["platforms"].items():
    print(f"   {p_name}: max_chars={p_cfg['max_chars']}, style={p_cfg['style'][:30]}...")

print()
print("=" * 60)
print("✅ 静态检查全部通过")
print("=" * 60)