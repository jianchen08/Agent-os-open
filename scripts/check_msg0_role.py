"""检查日志中是否有 system_message 为 None 的迹象。"""

import re

LOG_PATH = r"d:\myproject\container_08f57bc14532\logs\pipeline_81f98f451dc4.log"

# 搜索 MSG-0 的 role
with open(LOG_PATH, "r", encoding="utf-8") as f:
    for i, line in enumerate(f):
        m = re.search(r'\[llm_core\] MSG-0 role=(\w+)', line)
        if m:
            print(f"Line {i+1}: MSG-0 role={m.group(1)}")
            if i > 5000:
                break
