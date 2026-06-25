"""分析日志中高命中和低命中轮次的消息结构差异。"""
import re
import sys

LOG_FILE = r"d:\myproject\container_08f57bc14532\logs\pipeline_81f98f451dc4.log"

def parse_log(filepath):
    rounds = []
    current_round = None

    with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.rstrip("\n")

            m = re.search(r"\[llm_core\] Sending (\d+) messages to LLM", line)
            if m:
                if current_round:
                    rounds.append(current_round)
                current_round = {
                    "msg_count": int(m.group(1)),
                    "messages": [],
                    "cached_tokens": None,
                    "prompt_tokens": None,
                }

            m = re.search(r"\[llm_core\] MSG-(\d+) role=(\S+?)(?:\s+name=(\S+))?\s+content=(.*)", line)
            if m and current_round is not None:
                idx = int(m.group(1))
                role = m.group(2)
                name = m.group(3) or ""
                content = m.group(4)
                current_round["messages"].append({
                    "idx": idx,
                    "role": role,
                    "name": name,
                    "content_preview": content[:150],
                    "content_len": len(content),
                })

            m = re.search(r"'cached_tokens':\s*(\d+)", line)
            if m and current_round is not None:
                current_round["cached_tokens"] = int(m.group(1))

            m = re.search(r"'input_tokens':\s*(\d+)", line)
            if m and current_round is not None:
                current_round["prompt_tokens"] = int(m.group(1))

    if current_round:
        rounds.append(current_round)

    return rounds


def main():
    rounds = parse_log(LOG_FILE)
    print(f"Total rounds: {len(rounds)}")

    low_cache = []
    high_cache = []
    for i, r in enumerate(rounds):
        ct = r.get("cached_tokens") or 0
        pt = r.get("prompt_tokens") or 0
        if pt == 0:
            continue
        ratio = ct / pt if pt > 0 else 0
        r["ratio"] = ratio
        r["round_idx"] = i
        if ratio < 0.5:
            low_cache.append(r)
        elif ratio > 0.8:
            high_cache.append(r)

    print(f"\nLow cache rounds (<50%): {len(low_cache)}")
    for r in low_cache:
        print(f"  Round {r['round_idx']}: cached={r['cached_tokens']}, prompt={r['prompt_tokens']}, ratio={r['ratio']:.1%}, msgs={r['msg_count']}")

    print(f"\nHigh cache rounds (>80%): {len(high_cache)}")
    for r in high_cache[:5]:
        print(f"  Round {r['round_idx']}: cached={r['cached_tokens']}, prompt={r['prompt_tokens']}, ratio={r['ratio']:.1%}, msgs={r['msg_count']}")
    print(f"  ... ({len(high_cache)} total)")

    if not low_cache or not high_cache:
        print("Not enough data")
        return

    low = low_cache[0]
    high = high_cache[0]

    print(f"\n{'='*80}")
    print(f"Comparing: Low cache Round {low['round_idx']} vs High cache Round {high['round_idx']}")
    print(f"Low:  cached={low['cached_tokens']}, prompt={low['prompt_tokens']}, ratio={low['ratio']:.1%}")
    print(f"High: cached={high['cached_tokens']}, prompt={high['prompt_tokens']}, ratio={high['ratio']:.1%}")
    print(f"{'='*80}")

    low_msgs = {m["idx"]: m for m in low["messages"]}
    high_msgs = {m["idx"]: m for m in high["messages"]}

    max_idx = max(max(low_msgs.keys(), default=0), max(high_msgs.keys(), default=0))

    print(f"\n{'IDX':>4} | {'LOW role+name':<30} | {'HIGH role+name':<30} | {'Match'}")
    print("-" * 100)

    first_diff = None
    for idx in range(max_idx + 1):
        lm = low_msgs.get(idx)
        hm = high_msgs.get(idx)

        low_desc = f"{lm['role']} name={lm['name']}" if lm else "---"
        high_desc = f"{hm['role']} name={hm['name']}" if hm else "---"

        if lm and hm:
            if lm["role"] == hm["role"] and lm["name"] == hm["name"]:
                match = "✓"
            else:
                match = "✗ DIFF"
                if first_diff is None:
                    first_diff = idx
        elif lm and not hm:
            match = "← only in LOW"
            if first_diff is None:
                first_diff = idx
        elif hm and not lm:
            match = "→ only in HIGH"
            if first_diff is None:
                first_diff = idx
        else:
            match = ""

        print(f"{idx:>4} | {low_desc:<30} | {high_desc:<30} | {match}")

    if first_diff is not None:
        print(f"\nFirst difference at MSG-{first_diff}")

    print(f"\n{'='*80}")
    print("Low cache round - last 5 messages:")
    for m in low["messages"][-5:]:
        print(f"  MSG-{m['idx']}: role={m['role']} name={m['name']} len={m['content_len']} preview={m['content_preview'][:80]}")

    print(f"\nHigh cache round - last 5 messages:")
    for m in high["messages"][-5:]:
        print(f"  MSG-{m['idx']}: role={m['role']} name={m['name']} len={m['content_len']} preview={m['content_preview'][:80]}")

    print(f"\n{'='*80}")
    print("Low cache round - MSG-0 details:")
    if 0 in low_msgs:
        m = low_msgs[0]
        print(f"  role={m['role']} name={m['name']} len={m['content_len']}")
        print(f"  preview (last 200 chars): ...{m['content_preview'][-200:]}")

    print(f"\nHigh cache round - MSG-0 details:")
    if 0 in high_msgs:
        m = high_msgs[0]
        print(f"  role={m['role']} name={m['name']} len={m['content_len']}")
        print(f"  preview (last 200 chars): ...{m['content_preview'][-200:]}")

    print(f"\n{'='*80}")
    print("Comparing adjacent rounds (low cache round vs its PREVIOUS round):")
    if low["round_idx"] > 0:
        prev = rounds[low["round_idx"] - 1]
        prev_msgs = {m["idx"]: m for m in prev["messages"]}
        low_msgs_map = {m["idx"]: m for m in low["messages"]}

        max_idx2 = max(max(prev_msgs.keys(), default=0), max(low_msgs_map.keys(), default=0))
        first_diff2 = None
        for idx in range(max_idx2 + 1):
            pm = prev_msgs.get(idx)
            lm = low_msgs_map.get(idx)
            if pm and lm:
                if pm["role"] != lm["role"] or pm["name"] != lm["name"]:
                    first_diff2 = idx
                    break
            elif pm and not lm:
                first_diff2 = idx
                break
            elif lm and not pm:
                first_diff2 = idx
                break

        if first_diff2 is not None:
            print(f"  First difference at MSG-{first_diff2}")
            if first_diff2 in prev_msgs and first_diff2 in low_msgs_map:
                pm = prev_msgs[first_diff2]
                lm = low_msgs_map[first_diff2]
                print(f"  PREV: role={pm['role']} name={pm['name']} len={pm['content_len']} preview={pm['content_preview'][:100]}")
                print(f"  LOW:  role={lm['role']} name={lm['name']} len={lm['content_len']} preview={lm['content_preview'][:100]}")
        else:
            print(f"  All existing MSGs match by role+name (different counts: prev={len(prev['messages'])}, low={len(low['messages'])})")

        print(f"\n  PREV round: cached={prev.get('cached_tokens')}, prompt={prev.get('prompt_tokens')}, ratio={prev.get('ratio', 0):.1%}")
        print(f"  LOW round:  cached={low['cached_tokens']}, prompt={low['prompt_tokens']}, ratio={low['ratio']:.1%}")


if __name__ == "__main__":
    main()
