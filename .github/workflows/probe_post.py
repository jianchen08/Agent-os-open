"""探针结果发帖：读日志文件，抽 FAILED/--tb=line 错误行 + 尾部，POST 到 commit comment。

用法: python probe_post.py <log_path> <label>
（debug 分支专用文件，收完探针随分支删除，不落 main。）
"""

import json
import os
import sys
import urllib.request

log = open(sys.argv[1], encoding="utf-8", errors="replace").read()
label = sys.argv[2]
lines = log.splitlines()
failed = [l for l in lines if l.startswith(("FAILED", "ERROR"))]
tb = [l for l in lines if l.startswith("/") or ": AssertionError" in l or ": Exception" in l]
tail = lines[-25:]
nl = chr(10)
body = (
    label
    + " probe:"
    + nl
    + "```"
    + nl
    + nl.join(failed[:80])
    + nl
    + "--- tb ---"
    + nl
    + nl.join(tb[:80])
    + nl
    + "--- tail ---"
    + nl
    + nl.join(tail)
)[:60000]
body += nl + "```"
data = json.dumps({"body": body}).encode()
req = urllib.request.Request(
    "https://api.github.com/repos/jianchen08/Agent-os-open/commits/"
    + os.environ["GITHUB_SHA"]
    + "/comments",
    data=data,
    headers={
        "Authorization": "Bearer " + os.environ["GITHUB_TOKEN"],
        "Accept": "application/vnd.github+json",
    },
    method="POST",
)
with urllib.request.urlopen(req) as r:
    print("post status", r.status)
