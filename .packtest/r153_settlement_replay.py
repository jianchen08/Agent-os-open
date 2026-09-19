"""R153：用用户试玩的真实结算文本回放 _parse_settlement，验证 boss_fate=已陨落 兜底。"""
import importlib.util
import json
import os
import sys

sys.stdout = __import__("io").TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

PLUGIN = os.path.join(os.environ["APPDATA"], "agentos", "plugins", "modes", "mode_learning")
spec = importlib.util.spec_from_file_location("ml_server", os.path.join(PLUGIN, "server.py"))
ml = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ml)

# 用户试玩真实结算块（从 thread-6ccecac0 最后一条 assistant 消息提取的 ``` 块）
REAL = """```yaml
level_id: level_work_energy_01
boss: level_work_energy_lord
boss_fate: 已陨落
stages_completed: [recon, feynman, variant, berserk, hidden_teach]
knowledge_units_mastered:
  - work_definition
  - work_perpendicular_zero
  - power_definition
  - work_energy_theorem
  - friction_work_boundary
misconceptions_shattered:
  - 力大功多
  - 速度大合力大
  - 摩擦总减能
xp_total: 5
rank: S
next_unlock: level_work_energy_02（功率与机车 ·进阶谜阵）
```"""

r = ml._parse_settlement(REAL)
print("parse result:", json.dumps(r, ensure_ascii=False))
assert r is not None, "parser returned None"
assert r["completed"] is True, f"completed should be True, got {r['completed']}"
assert r["xp_awarded"] == 5, f"xp should be 5, got {r['xp_awarded']}"
assert r["level_id"] == "level_work_energy_01"

# 反向：删值实验——去掉 boss_fate 行，completed 应非 True（除非 result 兜底）
import re
NO_FATE = re.sub(r"boss_fate.*\n", "", REAL)
r2 = ml._parse_settlement(NO_FATE)
print("no-boss_fate:", json.dumps(r2, ensure_ascii=False) if r2 else None)
assert r2 is None or r2["completed"] is not True, "fallback must not fire without fate/result"

# 变体：英文 victory 与冒号全角
V = REAL.replace("boss_fate: 已陨落", "result：victory")
r3 = ml._parse_settlement(V)
assert r3 is not None and r3["completed"] is True, "victory variant failed"
print("victory variant: completed=True OK")
print("ALL PASS")
