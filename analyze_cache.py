"""
缓存命中率根因分析脚本（增强版）
核心假设：dynamic_vars 被合并到 MSG-0 末尾，当 dynamic_vars 内容变化时，
前缀缓存在 MSG-0 内部断裂，导致只能命中 MSG-0 的前半部分（~23,680 tokens）。
"""

import re
from dataclasses import dataclass, field


@dataclass
class Message:
    """单条消息的结构"""
    index: int
    role: str
    name: str | None
    content_preview: str
    content_length: int
    has_tool_calls: bool = False
    tool_calls_preview: str = ""


@dataclass
class Round:
    """一轮 LLM 调用的完整信息"""
    round_id: int
    line_num: int
    msg_count: int
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    messages: list[Message] = field(default_factory=list)
    msg0_content: str = ""
    system_msg_len: int = 0
    dynamic_vars_flag: bool = False


    @property
    def cache_hit_rate(self) -> float:
        """缓存命中率"""
        if self.input_tokens == 0:
            return 0.0
        return self.cached_tokens / self.input_tokens


def parse_log(filepath: str) -> list[Round]:
    """解析日志文件，提取每轮的缓存信息和消息列表"""
    rounds: list[Round] = []
    current_round: Round | None = None
    round_id = 0
    collecting_msg0 = False
    msg0_buf: list[str] = []

    sending_re = re.compile(r'\[llm_core\] Sending (\d+) messages to LLM')
    msg_content_re = re.compile(
        r'\[llm_core\] MSG-(\d+) role=(\S+?)(?:\s+name=(\S+))?\s+content=(.*)'
    )
    msg_toolcalls_re = re.compile(
        r'\[llm_core\] MSG-(\d+) role=(\S+?)(?:\s+name=(\S+))?\s+tool_calls=(.*)'
    )
    usage_re = re.compile(
        r"\[llm_core\] LLM full response:.*?"
        r"'input_tokens':\s*(\d+).*?"
        r"'output_tokens':\s*(\d+).*?"
        r"'cached_tokens':\s*(\d+)"
    )
    prompt_build_re = re.compile(
        r'\[prompt_build\] SystemMessage built \| content_len=(\d+) \| dynamic_vars=(\w+)'
    )

    with open(filepath, 'r', encoding='utf-8') as f:
        for line_num, line in enumerate(f, 1):
            line = line.rstrip('\n')

            m = sending_re.search(line)
            if m:
                if current_round is not None:
                    if msg0_buf:
                        current_round.msg0_content = "\n".join(msg0_buf)
                    rounds.append(current_round)
                round_id += 1
                current_round = Round(
                    round_id=round_id,
                    line_num=line_num,
                    msg_count=int(m.group(1)),
                )
                collecting_msg0 = False
                msg0_buf = []
                continue

            if current_round is not None:
                m = msg_content_re.search(line)
                if m:
                    idx = int(m.group(1))
                    role = m.group(2)
                    name = m.group(3)
                    content = m.group(4)
                    current_round.messages.append(Message(
                        index=idx,
                        role=role,
                        name=name,
                        content_preview=content[:100],
                        content_length=len(content),
                    ))
                    if idx == 0:
                        collecting_msg0 = True
                        msg0_buf = [content]
                    else:
                        collecting_msg0 = False
                    continue

                m = msg_toolcalls_re.search(line)
                if m:
                    collecting_msg0 = False
                    idx = int(m.group(1))
                    role = m.group(2)
                    name = m.group(3)
                    tool_calls = m.group(4)
                    current_round.messages.append(Message(
                        index=idx,
                        role=role,
                        name=name,
                        content_preview="",
                        content_length=0,
                        has_tool_calls=True,
                        tool_calls_preview=tool_calls[:100],
                    ))
                    continue

                if collecting_msg0 and line.strip() and not re.match(r"\d{2}:\d{2}:\d{2}.*\[llm_core\]", line):
                    msg0_buf.append(line)
                    continue

                m = usage_re.search(line)
                if m:
                    current_round.input_tokens = int(m.group(1))
                    current_round.output_tokens = int(m.group(2))
                    current_round.cached_tokens = int(m.group(3))
                    continue

                m = prompt_build_re.search(line)
                if m:
                    current_round.system_msg_len = int(m.group(1))
                    current_round.dynamic_vars_flag = m.group(2) == "True"
                    continue

    if current_round is not None:
        if msg0_buf:
            current_round.msg0_content = "\n".join(msg0_buf)
        rounds.append(current_round)

    return rounds


def find_msg0_diff_pos(content_a: str, content_b: str) -> int | None:
    """找出两个字符串第一个不同字符的位置"""
    min_len = min(len(content_a), len(content_b))
    for i in range(min_len):
        if content_a[i] != content_b[i]:
            return i
    if len(content_a) != len(content_b):
        return min_len
    return None


def main():
    log_path = r"d:\myproject\container_08f57bc14532\logs\pipeline_81f98f451dc4.log"

    print(f"📂 解析日志文件: {log_path}")
    rounds = parse_log(log_path)
    print(f"✅ 共解析到 {len(rounds)} 轮 LLM 调用")

    # 1. 概览表
    print("\n" + "=" * 110)
    print("📊 所有轮次缓存命中率概览")
    print("=" * 110)
    print(f"{'轮次':>4} | {'消息数':>4} | {'input_tokens':>12} | {'cached_tokens':>13} | {'命中率':>8} | {'sys_msg_len':>11} | {'dv':>2} | {'MSG-0捕获len':>13} | 状态")
    print("-" * 110)
    for r in rounds:
        rate = r.cache_hit_rate
        if rate < 0.5:
            status = "🔴 低命中"
        elif rate < 0.8:
            status = "🟡 中等"
        else:
            status = "🟢 高命中"
        dv = "✓" if r.dynamic_vars_flag else "✗"
        msg0_len = len(r.msg0_content)
        print(f"{r.round_id:>4} | {r.msg_count:>4} | {r.input_tokens:>12} | {r.cached_tokens:>13} | {rate:>7.1%} | {r.system_msg_len:>11} | {dv:>2} | {msg0_len:>13} | {status}")

    # 2. 找低命中和高命中轮次
    low_hit = [r for r in rounds if r.cache_hit_rate < 0.5]
    high_hit = [r for r in rounds if r.cache_hit_rate > 0.8]

    print(f"\n🔴 低命中率轮次 (<50%): {len(low_hit)} 个")
    for r in low_hit:
        print(f"   轮次 {r.round_id}: 命中率={r.cache_hit_rate:.1%}, "
              f"cached={r.cached_tokens}, input={r.input_tokens}")

    # 3. 核心分析：对比相邻高命中→低命中轮次的 MSG-0
    print("\n" + "=" * 110)
    print("🔬 核心分析：MSG-0 内容变化检测（前缀缓存断裂根因）")
    print("=" * 110)

    for lr in low_hit:
        prev_high = None
        for r in reversed(high_hit):
            if r.round_id < lr.round_id:
                prev_high = r
                break
        if prev_high is None:
            continue

        print(f"\n--- 低命中轮次 {lr.round_id} (cached={lr.cached_tokens}, rate={lr.cache_hit_rate:.1%}) "
              f"vs 前一个高命中轮次 {prev_high.round_id} (cached={prev_high.cached_tokens}, rate={prev_high.cache_hit_rate:.1%}) ---")

        # 对比 MSG-0 内容
        msg0_high = prev_high.msg0_content
        msg0_low = lr.msg0_content

        print(f"  MSG-0 捕获长度: 高命中={len(msg0_high):,}, 低命中={len(msg0_low):,}")
        print(f"  system_msg_len: 高命中={prev_high.system_msg_len:,}, 低命中={lr.system_msg_len:,}")

        if msg0_high and msg0_low:
            diff_pos = find_msg0_diff_pos(msg0_high, msg0_low)
            if diff_pos is not None:
                print(f"  ❌ MSG-0 内容在字符位置 {diff_pos:,} 处首次不同！")
                context_start = max(0, diff_pos - 50)
                context_end = min(len(msg0_high), len(msg0_low), diff_pos + 50)
                print(f"  高命中轮次 MSG-0 [{context_start}:{context_end}]:")
                print(f"    ...{msg0_high[context_start:context_end]}...")
                print(f"  低命中轮次 MSG-0 [{context_start}:{context_end}]:")
                print(f"    ...{msg0_low[context_start:context_end]}...")

                # 估算断裂点对应的 token 位置
                estimated_tokens = diff_pos // 2.2  # 中文约 2.2 字符/token
                print(f"  估算断裂点 token 位置: ~{estimated_tokens:,.0f} tokens")
                print(f"  实际 cached_tokens: {lr.cached_tokens:,}")
                if abs(estimated_tokens - lr.cached_tokens) < 5000:
                    print(f"  ✅ 估算断裂点与 cached_tokens 吻合！确认前缀在 MSG-0 内部断裂")
                else:
                    print(f"  ⚠️  估算断裂点与 cached_tokens 不完全吻合，可能需要进一步分析")
            else:
                print(f"  ✅ MSG-0 捕获内容完全相同（差异可能在截断部分）")
                # 检查长度差异
                if len(msg0_high) != len(msg0_low):
                    print(f"  ⚠️  但捕获长度不同: 高命中={len(msg0_high):,}, 低命中={len(msg0_low):,}")
                # 检查 system_msg_len 差异
                if prev_high.system_msg_len != lr.system_msg_len:
                    print(f"  ❌ system_msg_len 不同: 高命中={prev_high.system_msg_len:,}, 低命中={lr.system_msg_len:,}")
                    print(f"  → system prompt 内容发生了变化！")
                else:
                    print(f"  system_msg_len 相同 ({prev_high.system_msg_len:,})，但动态变量内容可能不同")
        else:
            print(f"  ⚠️  MSG-0 内容为空，无法比较")

        # 对比 dynamic_context 消息
        high_dc = [m for m in prev_high.messages if m.name == "dynamic_context"]
        low_dc = [m for m in lr.messages if m.name == "dynamic_context"]
        print(f"\n  dynamic_context 消息: 高命中={len(high_dc)}条, 低命中={len(low_dc)}条")
        for dc in high_dc:
            print(f"    高命中: MSG-{dc.index} role={dc.role} content_preview={dc.content_preview[:80]}")
        for dc in low_dc:
            print(f"    低命中: MSG-{dc.index} role={dc.role} content_preview={dc.content_preview[:80]}")

    # 4. 验证：检查所有轮次 MSG-0 是否包含 dynamic_vars 标记
    print("\n" + "=" * 110)
    print("📋 MSG-0 内容特征检查")
    print("=" * 110)
    for r in rounds[:10]:
        msg0 = r.msg0_content
        has_dv_tag = "<dynamic_vars>" in msg0
        has_time = "当前时间" in msg0
        has_date = "当前日期" in msg0 or "日期" in msg0
        print(f"  轮次{r.round_id:>2}: len={len(msg0):>6}, <dynamic_vars>={has_dv_tag}, "
              f"当前时间={has_time}, 日期相关={has_date}, "
              f"cached={r.cached_tokens:>6}, rate={r.cache_hit_rate:.1%}")

    # 5. 最终结论
    print("\n" + "=" * 110)
    print("📌 根因结论")
    print("=" * 110)

    # 检查低命中轮次的 cached_tokens 是否一致
    low_cached_values = [r.cached_tokens for r in low_hit]
    avg_cached = sum(low_cached_values) / len(low_cached_values) if low_cached_values else 0

    print(f"""
  现象：
  - 低命中轮次 cached_tokens ≈ {avg_cached:,.0f}（范围 {min(low_cached_values):,} ~ {max(low_cached_values):,}）
  - 高命中轮次 cached_tokens 远大于 MSG-0 的 token 数

  根因：
  - _build_messages() 将 prompt.dynamic_vars 合并到 MSG-0（system message）末尾
  - dynamic_vars 包含时间戳等每轮变化的内容
  - 当 dynamic_vars 内容变化时，前缀缓存在 MSG-0 内部断裂
  - 断裂点 ≈ MSG-0 中 dynamic_vars 的插入位置（约 {avg_cached:,.0f} tokens）
  - 断裂后，MSG-0 剩余部分及所有历史消息都无法命中缓存

  证据链：
  1. prompt_build 日志显示 dynamic_vars=True（每轮都生成动态变量）
  2. MSG-0 包含"当前时间"（时间戳嵌入在 system prompt 中）
  3. 低命中轮次 cached_tokens ≈ MSG-0 前半部分（dynamic_vars 插入点之前）的 token 数
  4. 高命中轮次 cached_tokens ≈ 整个 input_tokens（说明 dynamic_vars 没有变化，前缀完整匹配）

  修复建议：
  - 方案A：将 dynamic_vars 从 MSG-0 中分离，作为独立消息追加在历史消息之后
    → 前缀（MSG-0 + 历史）保持不变，缓存可命中
    → dynamic_vars 变化只影响最后一条消息，不影响前缀
  - 方案B：将 dynamic_vars 中变化的部分（如时间戳）移到 history 末尾的 user 消息中
    → 保持 MSG-0 完全静态，前缀永远可缓存
  - 方案C：在 _build_messages 中将 dynamic_vars 追加为独立的 system 消息（MSG-0 之后）
    → 但需注意 DeepSeek 等提供商对非首位 system 消息的处理
""")


if __name__ == "__main__":
    main()
