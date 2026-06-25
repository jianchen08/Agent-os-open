"""提取 MSG-0 的最后 500 字符，看是否包含 dynamic_vars 内容。"""

import re

LOG_PATH = r"d:\myproject\container_08f57bc14532\logs\pipeline_81f98f451dc4.log"

RE_MSG = re.compile(r"\[llm_core\] MSG-(\d+)\s+(.*)")
RE_SENDING = re.compile(r"\[llm_core\] Sending (\d+) messages")
RE_ITER = re.compile(r"\[llm_core\] pipeline=\w+\s+iter=(\d+)\s+LLM returned")


def main():
    cur_msg_idx = None
    cur_buf = []
    sending = False
    iter_count = 0

    with open(LOG_PATH, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.rstrip("\n")

            m = RE_SENDING.search(line)
            if m:
                cur_msg_idx = None
                cur_buf = []
                sending = True
                continue

            if sending:
                m2 = RE_MSG.search(line)
                if m2:
                    if cur_msg_idx == 0:
                        content = "\n".join(cur_buf)
                        print(f"\n=== Iter {iter_count+1} MSG-0 末尾 500 字符 ===")
                        print(content[-500:])
                        print(f"总长度: {len(content):,}")
                        iter_count += 1
                        if iter_count >= 3:
                            return

                    cur_msg_idx = int(m2.group(1))
                    first_line = m2.group(2)
                    if cur_msg_idx == 0:
                        cur_buf = [first_line]
                    else:
                        cur_buf = []
                        cur_msg_idx = None
                    continue

                if cur_msg_idx == 0 and line.strip() and not re.match(r"\d{2}:\d{2}:\d{2}.*\[llm_core\]", line):
                    cur_buf.append(line)

            m3 = RE_ITER.search(line)
            if m3:
                if cur_msg_idx == 0:
                    content = "\n".join(cur_buf)
                    print(f"\n=== Iter {iter_count+1} MSG-0 末尾 500 字符 ===")
                    print(content[-500:])
                    print(f"总长度: {len(content):,}")
                    iter_count += 1
                    if iter_count >= 3:
                        return
                cur_msg_idx = None
                cur_buf = []
                sending = False


if __name__ == "__main__":
    main()
