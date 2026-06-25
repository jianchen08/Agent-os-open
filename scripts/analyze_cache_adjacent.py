"""对比相邻的高低命中轮次，找出前缀断裂点。"""
import re

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
                    "content_preview": content[:200],
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


def compare_rounds(rounds, low_idx, prev_idx):
    low = rounds[low_idx]
    prev = rounds[prev_idx]
    print(f"\n{'='*80}")
    print(f"Round {prev_idx} (PREV): cached={prev.get('cached_tokens')}, prompt={prev.get('prompt_tokens')}")
    print(f"Round {low_idx} (LOW):  cached={low.get('cached_tokens')}, prompt={low.get('prompt_tokens')}")
    print(f"{'='*80}")

    prev_msgs = prev["messages"]
    low_msgs = low["messages"]

    min_len = min(len(prev_msgs), len(low_msgs))
    first_diff = None
    for i in range(min_len):
        pm = prev_msgs[i]
        lm = low_msgs[i]
        if pm["role"] != lm["role"] or pm["name"] != lm["name"]:
            first_diff = i
            print(f"\nFirst role/name difference at position {i} (MSG-{pm['idx']} vs MSG-{lm['idx']}):")
            print(f"  PREV: role={pm['role']} name={pm['name']} len={pm['content_len']} preview={pm['content_preview'][:100]}")
            print(f"  LOW:  role={lm['role']} name={lm['name']} len={lm['content_len']} preview={lm['content_preview'][:100]}")
            break

    if first_diff is None:
        print(f"\nNo role/name difference in first {min_len} messages")
        print(f"PREV has {len(prev_msgs)} msgs, LOW has {len(low_msgs)} msgs")

    print(f"\n--- PREV round last 8 messages ---")
    for m in prev_msgs[-8:]:
        print(f"  MSG-{m['idx']}: role={m['role']} name={m['name']} len={m['content_len']} preview={m['content_preview'][:80]}")

    print(f"\n--- LOW round last 8 messages ---")
    for m in low_msgs[-8:]:
        print(f"  MSG-{m['idx']}: role={m['role']} name={m['name']} len={m['content_len']} preview={m['content_preview'][:80]}")

    print(f"\n--- LOW round messages around the 'dynamic_context' position ---")
    dc_idx = None
    for i, m in enumerate(low_msgs):
        if m["name"] == "dynamic_context":
            dc_idx = i
            break
    if dc_idx is not None:
        start = max(0, dc_idx - 3)
        end = min(len(low_msgs), dc_idx + 2)
        for i in range(start, end):
            m = low_msgs[i]
            marker = " <<<< dynamic_context" if m["name"] == "dynamic_context" else ""
            print(f"  [{i}] MSG-{m['idx']}: role={m['role']} name={m['name']} len={m['content_len']} preview={m['content_preview'][:80]}{marker}")

    print(f"\n--- PREV round messages around the 'dynamic_context' position ---")
    dc_idx2 = None
    for i, m in enumerate(prev_msgs):
        if m["name"] == "dynamic_context":
            dc_idx2 = i
            break
    if dc_idx2 is not None:
        start = max(0, dc_idx2 - 3)
        end = min(len(prev_msgs), dc_idx2 + 2)
        for i in range(start, end):
            m = prev_msgs[i]
            marker = " <<<< dynamic_context" if m["name"] == "dynamic_context" else ""
            print(f"  [{i}] MSG-{m['idx']}: role={m['role']} name={m['name']} len={m['content_len']} preview={m['content_preview'][:80]}{marker}")


def main():
    rounds = parse_log(LOG_FILE)
    print(f"Total rounds: {len(rounds)}")

    for i, r in enumerate(rounds):
        ct = r.get("cached_tokens") or 0
        pt = r.get("prompt_tokens") or 0
        ratio = ct / pt if pt > 0 else 0
        r["ratio"] = ratio
        r["round_idx"] = i

    low_indices = []
    for i, r in enumerate(rounds):
        if r["ratio"] < 0.5:
            low_indices.append(i)

    print(f"\nLow cache rounds: {low_indices}")

    for li in low_indices:
        if li > 0:
            compare_rounds(rounds, li, li - 1)


if __name__ == "__main__":
    main()
