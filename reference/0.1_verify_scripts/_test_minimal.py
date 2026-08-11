"""独立测试：只测试最基础的 YAML 加载和字段访问"""
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import yaml

data_file = SCRIPT_DIR / "resume_data.yaml"
with open(data_file, "r", encoding="utf-8") as f:
    data = yaml.safe_load(f)

print("✅ YAML 加载成功")
print(f"   meta.version: {data['meta']['version']}")
print(f"   theme.default: {data['theme']['default']}")
print(f"   basic.name_zh: {data['basic']['name_zh']}")
print(f"   basic.display_name_zh: {data['basic']['display_name_zh']}")
print(f"   basic.birth_year: {data['basic']['birth_year']}")
print(f"   basic.gender_zh: {data['basic']['gender_zh']}")
print(f"   job.target_roles_zh: {data['job_intention']['target_roles_zh']}")
print(f"   projects count: {len(data['projects'])}")
print(f"   featured project: {data['projects'][0]['name_zh']}")
print(f"   featured project responsibilities (count): {len(data['projects'][0]['responsibilities_zh'])}")
print(f"   featured project highlights (count): {len(data['projects'][0]['highlights_zh'])}")
print(f"   featured project metrics (count): {len(data['projects'][0]['metrics_zh'])}")
print(f"   featured project tech_stack: {len(data['projects'][0]['tech_stack'])}")
print(f"   skills categories: {len(data['skills']['categories'])}")
print(f"   education count: {len(data['education'])}")
print(f"   open_source.contributions_zh: {data['open_source']['contributions_zh']}")
print(f"   awards status: {data['awards']['status']}")
print(f"   about summary_zh length: {len(data['about']['summary_zh'])} chars")
print(f"   layout sections_order: {data['layout']['sections_order']}")
print(f"   platforms.boss_zhipin max_chars: {data['platforms']['boss_zhipin']['max_chars']}")
print("\n✅ 所有关键字段读取正常")