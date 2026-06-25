"""验证：MSG-0 是否包含 dynamic_vars 内容？
如果包含，说明 prompt.dynamic_vars 被合并了，
而 history 中的 dynamic_context 是另一份。"""

import re

LOG_PATH = r"d:\myproject\container_08f57bc14532\logs\pipeline_81f98f451dc4.log"

RE_MSG = re.compile(r"\[llm_core\] MSG-(\d+)\s+(.*)")
RE_SENDING = re.compile(r"\[llm_core\] Sending (\d+) messages")
RE_ITER = re.compile(r"\[llm_core\] pipeline=\w+\s+iter=(\d+)\s+LLM returned")


def main():
    """提取前几轮的 MSG-0 完整内容，看是否包含 dynamic_vars。"""
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
                    # 先 flush 之前的
                    if cur_msg_idx is not None:
                        content = "\n".join(cur_buf)
                        if cur_msg_idx == 0:
                            has_dv = "<dynamic_vars>" in content
                            has_time = "当前时间" in content
                            print(f"MSG-0: len={len(content):,}, 包含<dynamic_vars>={has_dv}, 包含当前时间={has_time}")
                            if has_dv:
                                # 找到 dynamic_vars 在 MSG-0 中的位置
                                pos = content.find("<dynamic_vars>")
                                print(f"  <dynamic_vars> 在 MSG-0 中的位置: 字符 {pos:,} / {len(content):,} ({pos/len(content)*100:.1f}%)")
                            iter_count += 1
                            if iter_count >= 5:
                                return

                    cur_msg_idx = int(m2.group(1))
                    first_line = m2.group(2)
                    if cur_msg_idx == 0:
                        cur_buf = [first_line]
                    else:
                        cur_buf = []
                        if cur_msg_idx > 2:
                            # 不需要收集非 MSG-0 的内容
                            cur_msg_idx = None
                    continue

                if cur_msg_idx == 0 and line.strip() and not re.match(r"\d{2}:\d{2}:\d{2}.*\[llm_core\]", line):
                    cur_buf.append(line)

            m3 = RE_ITER.search(line)
            if m3:
                if cur_msg_idx == 0:
                    content = "\n".join(cur_buf)
                    has_dv = "<dynamic_vars>" in content
                    has_time = "当前时间" in content
                    print(f"MSG-0: len={len(content):,}, 包含<dynamic_vars>={has_dv}, 包含当前时间={has_time}")
                    if has_dv:
                        pos = content.find("<dynamic_vars>")
                        print(f"  <dynamic_vars> 在 MSG-0 中的位置: 字符 {pos:,} / {len(content):,} ({pos/len(content)*100:.1f}%)")
                    iter_count += 1
                    if iter_count >= 5:
                        return
                cur_msg_idx = None
                cur_buf = []
                sending = False


if __name__ == "__main__":
    main()
