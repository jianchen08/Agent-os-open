import re
import yaml

# 模拟 security_check 加载：YAML 双引号 -> Python str -> re.search
yaml_doc = '''
rules:
  - name: "host_path_access_old"
    patterns:
      - type: regex
        value: "[A-Za-z]:[\\\\/]"
  - name: "host_path_access_new"
    patterns:
      - type: regex
        value: "(?:^|[\\s\\\"'=`])([A-Za-z]):[\\\\/]"
'''
data = yaml.safe_load(yaml_doc)
old_val = data["rules"][0]["patterns"][0]["value"]
new_val = data["rules"][1]["patterns"][0]["value"]
print("OLD value repr:", repr(old_val))
print("NEW value repr:", repr(new_val))

old_re = re.compile(old_val)
new_re = re.compile(new_val)

tests = [
    ("ls D:/myproject/", True),
    ("curl http://example.com", False),
    ("git clone https://github.com/x/y.git", False),
    ("ls D:\\myproject", True),
]
print()
for cmd, expect in tests:
    old_m = bool(old_re.search(cmd))
    new_m = bool(new_re.search(cmd))
    print(f"expect={expect!s:5} | OLD={'MATCH' if old_m else 'no':5} NEW={'MATCH' if new_m else 'no':5} | {cmd!r}")
