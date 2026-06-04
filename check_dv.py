"""检查 dynamic_vars 中 execution_review_rules 是否被注入"""
import sys, yaml
sys.path.insert(0, ".")
from scripts.agent_prompt_viewer import _run_worker

# 随便测一个 agent
with open("config/agents/main/lingxi.yaml", encoding="utf-8") as f:
    cfg = yaml.safe_load(f)
r = _run_worker(cfg, "")
dv = r["dynamic_vars"]
print(f"dynamic_vars 长度: {len(dv)}")
print(f"含 'execution_review_rules': {'execution_review_rules' in dv}")
print(f"含 '确认范围': {'确认范围' in dv}")
print(f"含 '回顾流程': {'回顾流程' in dv}")
if dv:
    print(f"前 300 字符: {dv[:300]!r}")
else:
    print("dynamic_vars 为空!")
