"""检查日志中 MSG-0 的实际内容长度，判断 dynamic_vars 是否被合并。"""

import re

LOG_PATH = r"d:\myproject\container_08f57bc14532\logs\pipeline_81f98f451dc4.log"

RE_MSG = re.compile(r"\[llm_core\] MSG-(\d+)\s+(.*)")
RE_SENDING = re.compile(r"\[llm_core\] Sending (\d+) messages")
RE_ITER = re.compile(r"\[llm_core\] pipeline=\w+\s+iter=(\d+)\s+LLM returned")
RE_SYSTEM_BUILT = re.compile(r"\[prompt_build\] SystemMessage built \| content_len=(\d+) \| dynamic_vars=(\w+)")


def main():
    """对比 prompt_build 报告的 system_content 长度和 MSG-0 实际长度。"""
    cur_msg0_len = 0
    cur_msg_idx = None
    cur_buf = []
    sending = False
    iter_count = 0
    system_len = 0
    has_dyn = False

    with open(LOG_PATH, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.rstrip("\n")

            m = RE_SYSTEM_BUILT.search(line)
            if m:
                system_len = int(m.group(1))
                has_dyn = m.group(2) == "True"
                continue

            m = RE_SENDING.search(line)
            if m:
                cur_msg0_len = 0
                cur_msg_idx = None
                cur_buf = []
                sending = True
                continue

            if sending:
                m2 = RE_MSG.search(line)
                if m2:
                    if cur_msg_idx == 0:
                        content = "\n".join(cur_buf)
                        cur_msg0_len = len(content)

                    cur_msg_idx = int(m2.group(1))
                    first_line = m2.group(2)
                    if cur_msg_idx == 0:
                        cur_buf = [first_line]
                    else:
                        cur_buf = []
                        if cur_msg_idx > 2:
                            cur_msg_idx = None
                    continue

                if cur_msg_idx == 0 and line.strip() and not re.match(r"\d{2}:\d{2}:\d{2}.*\[llm_core\]", line):
                    cur_buf.append(line)

            m3 = RE_ITER.search(line)
            if m3:
                if cur_msg_idx == 0:
                    content = "\n".join(cur_buf)
                    cur_msg0_len = len(content)

                iter_count += 1
                if iter_count <= 5:
                    diff = cur_msg0_len - system_len
                    print(f"Iter {iter_count}: system_content_len={system_len}, MSG-0实际长度={cur_msg0_len}, 差值={diff}, has_dynamic={has_dyn}")
                    if diff > 0:
                        print(f"  MSG-0 比 system_content 长 {diff} 字符 → dynamic_vars 可能被合并了")
                    elif diff < 0:
                        print(f"  MSG-0 比 system_content 短 {-diff} 字符 → 可能被截断")
                    else:
                        print(f"  MSG-0 和 system_content 一样长 → dynamic_vars 没被合并")

                cur_msg0_len = 0
                cur_msg_idx = None
                cur_buf = []
                sending = False

                if iter_count >= 5:
                    break


if __name__ == "__main__":
    main()
