#!/usr/bin/env python3
"""验证全部 44 个管道插件的文件完整性。"""
import json
import os
import sys

BASE = os.path.join(os.path.dirname(__file__), "..", "..", "plugins", "shared", "pipeline")
CATEGORIES = ["input", "output", "core"]
errors = []
ok_count = 0

for cat in CATEGORIES:
    cat_dir = os.path.join(BASE, cat)
    if not os.path.isdir(cat_dir):
        continue
    for name in sorted(os.listdir(cat_dir)):
        plugin_dir = os.path.join(cat_dir, name)
        if not os.path.isdir(plugin_dir):
            continue
        # 检查三个必需文件
        plugin_py = os.path.join(plugin_dir, "plugin.py")
        server_py = os.path.join(plugin_dir, "server.py")
        plugin_json = os.path.join(plugin_dir, "plugin.json")

        issues = []
        for f, label in [(plugin_py, "plugin.py"), (server_py, "server.py"), (plugin_json, "plugin.json")]:
            if not os.path.exists(f):
                issues.append(f"missing {label}")
            elif os.path.getsize(f) == 0:
                issues.append(f"{label} is empty")

        # 验证 plugin.json 格式
        if not issues:
            try:
                with open(plugin_json) as fh:
                    data = json.load(fh)
                if data.get("plugin_type") != "pipeline":
                    issues.append(f"plugin_type != pipeline (got {data.get('plugin_type')})")
                if not data.get("id"):
                    issues.append("missing id field")
                if not data.get("entry"):
                    issues.append("missing entry field")
            except Exception as e:
                issues.append(f"plugin.json parse error: {e}")

        # 验证 server.py 有关键结构
        if not issues:
            try:
                with open(server_py) as fh:
                    content = fh.read()
                checks = [
                    ("AgentOSPlugin", "AgentOSPlugin" in content),
                    ("on_load", "@plugin.on_load" in content),
                    ("plugin.run()", "plugin.run()" in content),
                    ("from plugin import", "from plugin import" in content),
                ]
                for label, passed in checks:
                    if not passed:
                        issues.append(f"server.py missing: {label}")
            except Exception as e:
                issues.append(f"server.py read error: {e}")

        if issues:
            errors.append(f"{cat}/{name}: {'; '.join(issues)}")
        else:
            ok_count += 1
            print(f"  OK: {cat}/{name}")

print(f"\n=== Validation: {ok_count} OK, {len(errors)} errors ===")
if errors:
    print("\nERRORS:")
    for e in errors:
        print(f"  {e}")
    sys.exit(1)
else:
    print("\nAll plugins validated successfully!")
