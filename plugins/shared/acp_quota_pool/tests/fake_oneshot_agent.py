#!/usr/bin/env python3
"""测试用 fake one-shot CLI——`zcode -p` 结果面行为替身（零第三方依赖）。

被 test_acp_dispatch_oneshot.py 以子进程拉起（sys.executable），对引擎的
oneshot 派发路径回真实进程语义（exit code / stdout / stderr）。行为变体经
argv 模式开关（default=echo）：

- echo（缺省）：stdout 打印 JSON 结果对象（response=argv[2] 全文——引擎已把
  注册表 args 里的 {goal} 占位符替换为派发目标，占位符替换链路由此断言），exit 0；
- plain：stdout 打印非 JSON 原文（容错解析回退路径），exit 0；
- badjson：stdout 打印以 { 开头但非法 JSON 的文本（解析失败回退路径），exit 0；
- noresponse：stdout 打印合法 JSON 但无 response 字段（字段缺失回退路径），exit 0；
- empty：stdout/stderr 全空，exit 0（无输出 = 无结果，引擎判 failed）；
- fail：stderr 打印错误行，exit 3（非零退出 = failed，不挂墙）；
- quota：stderr 打印额度措辞错误行，exit 4（撞墙归类断言语料）；
- slow：延迟 N 秒（argv[2]，缺省 5）后 echo（超时 kill 路径断言）。

诊断一律走 stderr 且除 fail/quota 模式外不写；IO 走字节流（UTF-8，不沾控制台
代码页）。
"""

from __future__ import annotations

import json
import sys
import time


def _mode() -> str:
    return sys.argv[1] if len(sys.argv) > 1 else "echo"


def _goal() -> str:
    """echo 模式的派发目标 = argv[2]（引擎占位符替换后的实参）。"""
    return sys.argv[2] if len(sys.argv) > 2 else ""


def _write(text: str) -> None:
    sys.stdout.buffer.write(text.encode("utf-8"))
    sys.stdout.buffer.flush()


def main() -> None:
    mode = _mode()
    if mode == "slow":
        time.sleep(float(sys.argv[2]) if len(sys.argv) > 2 else 5.0)
    if mode == "fail":
        sys.stderr.buffer.write(b"fake one-shot exploded\n")
        sys.stderr.buffer.flush()
        raise SystemExit(3)
    if mode == "quota":
        sys.stderr.buffer.write(b"usage limit reached: quota exhausted (429)\n")
        sys.stderr.buffer.flush()
        raise SystemExit(4)
    if mode == "plain":
        _write("plain-text-answer no json here")
        return
    if mode == "badjson":
        _write("{definitely not json")
        return
    if mode == "noresponse":
        _write(json.dumps({"ok": True, "other": "field"}))
        return
    if mode == "empty":
        return
    _write(json.dumps({"response": f"oneshot:{_goal()}"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
