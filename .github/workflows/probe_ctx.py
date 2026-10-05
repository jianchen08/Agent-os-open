"""探针上下文提取：INTERNALERROR 前 150 行 + FAILED 清单 + 尾部。

用法: python probe_ctx.py <log_path> <label>
"""

import json
import os
import sys
import urllib.request

with open(sys.argv[1], encoding="utf-8", errors="replace") as f:
    lines = f.read().splitlines()
label = sys.argv[2]
failed = [ln for ln in lines if ln.startswith(("FAILED", "ERROR"))]
ie = [i for i, ln in enumerate(lines) if ln.startswith("INTERNALERROR")]
ctx = lines[max(0, ie[0] - 150) : ie[0] + 5] if ie else []
tail = lines[-20:]
nl = chr(10)
body = (
    label
    + " probe:"
    + nl
    + "```"
    + nl
    + "INTERNALERROR count: "
    + str(len(ie))
    + nl
    + "--- FAILED ---"
    + nl
    + nl.join(failed[:60])
    + nl
    + "--- ctx before first INTERNALERROR (150) ---"
    + nl
    + nl.join(ctx)
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
