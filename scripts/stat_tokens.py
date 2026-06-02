"""统计日志中每轮的 token 用量。"""
import re
import sys

LOG = sys.argv[1] if len(sys.argv) > 1 else r"logs\pipeline_9b15a2cf1255.log"

rounds = []
cur = None

with open(LOG, "r", encoding="utf-8", errors="ignore") as f:
    for line in f:
        line = line.rstrip()
        m = re.search(r"\[llm_core\] Sending (\d+) messages to LLM", line)
        if m:
            if cur:
                rounds.append(cur)
            cur = {"msgs": int(m.group(1)), "cached": None, "input": None, "output": None, "total": None}
        m = re.search(r"'input_tokens':\s*(\d+)", line)
        if m and cur:
            cur["input"] = int(m.group(1))
        m = re.search(r"'output_tokens':\s*(\d+)", line)
        if m and cur:
            cur["output"] = int(m.group(1))
        m = re.search(r"'total_tokens':\s*(\d+)", line)
        if m and cur:
            cur["total"] = int(m.group(1))
        m = re.search(r"'cached_tokens':\s*(\d+)", line)
        if m and cur:
            cur["cached"] = int(m.group(1))
if cur:
    rounds.append(cur)

print(f"Total rounds: {len(rounds)}")
header = f"{'Round':>5} | {'Input':>8} | {'Output':>7} | {'Cached':>8} | {'Total':>8} | {'Cache%':>7} | {'Msgs':>4}"
print(header)
print("-" * len(header))

total_in = total_out = total_cached = 0
for i, r in enumerate(rounds):
    inp = r["input"] or 0
    out = r["output"] or 0
    cached = r["cached"] or 0
    total_r = r["total"] or 0
    msgs = r["msgs"]
    ratio = cached / inp * 100 if inp else 0
    total_in += inp
    total_out += out
    total_cached += cached
    print(f"{i:>5} | {inp:>8,} | {out:>7,} | {cached:>8,} | {total_r:>8,} | {ratio:>6.1f}% | {msgs:>4}")

print("-" * len(header))
ratio_all = total_cached / total_in * 100 if total_in else 0
print(f"{'TOTAL':>5} | {total_in:>8,} | {total_out:>7,} | {total_cached:>8,} | {'':>8} | {ratio_all:>6.1f}% |")
