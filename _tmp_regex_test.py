import re

# 改进后的正则（从 plugin.py 复制）
HOST_PATH_RE = re.compile(r"(?:^|[\s\"'=`])([A-Za-z]):[\\/]")

tests = [
    # 应命中：宿主路径
    ("ls D:/myproject/", True),
    ("ls D:\\myproject", True),
    ("cat C:/Users/jc/file.txt", True),
    ("dir E:\\data", True),
    ("cd F:\\foo\\bar", True),
    ('"D:/my dir/"', True),            # 引号包裹路径
    ("ls  D:/x", True),                # 多空格
    # 不应命中：URL（最关键）
    ("curl http://example.com", False),
    ("wget https://example.com", False),
    ("git clone https://github.com/x/y.git", False),
    # 不应命中：普通命令
    ("echo hello", False),
    ("ls -la", False),
    ("git status", False),
    ("cd /workspace/src", False),
    ("python script.py", False),
    ("ls", False),
    ("D:", False),                     # 只有盘符没有分隔符
    ("pip install requests", False),
]

ok = True
for cmd, expect in tests:
    got = bool(HOST_PATH_RE.search(cmd))
    status = "OK  " if got == expect else "FAIL"
    if got != expect:
        ok = False
    print(f"{status} | expect={expect!s:5} got={got!s:5} | {cmd!r}")

print("=== ALL PASS ===" if ok else "=== HAS FAILURES ===")
