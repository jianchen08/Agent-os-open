"""探针结果发帖：读日志文件，抽 FAILED 清单 + FAILURES 段头部 + 尾部，POST 到 commit comment。

用法: python probe_post.py <log_path> <label>
（debug 分支专用文件，收完探针随分支删除，不落 main。）
"""

import json
import os
import sys
import urllib.request

with open(sys.argv[1], encoding="utf-8", errors="replace") as f:
    log = f.read()
label = sys.argv[2]
lines = log.splitlines()
failed = [ln for ln in lines if ln.startswith(("FAILED", "ERROR"))]
# FAILURES 段：从首个 ============ FAILURES 到段尾（短测报直接截头部 200 行）
try:
    start = next(i for i, ln in enumerate(lines) if "FAILURES" in ln and ln.startswith("="))
    failures_block = lines[start : start + 200]
except StopIteration:
    failures_block = []
tail = lines[-30:]
nl = chr(10)
body = (
    label
    + " probe:"
    + nl
    + "```"
    + nl
    + nl.join(failed[:80])
    + nl
    + "--- failures block (head 200) ---"
    + nl
    + nl.join(failures_block)
    + nl
    + "--- tail ---"
    + nl
    + nl.join(tail)
)[:60000]
body += nl + "```"
data = json.dumps({"body": body}).encode()
req = urllib.request.Request(
    "https://api.github.com/repos/jianchen08/Agent-os-open/commits/" + os.environ["GITHUB_SHA"] + "/comments",
    data=data,
    headers={
        "Authorization": "Bearer " + os.environ["GITHUB_TOKEN"],
        "Accept": "application/vnd.github+json",
    },
    method="POST",
)
with urllib.request.urlopen(req) as r:
    print("post status", r.status)
