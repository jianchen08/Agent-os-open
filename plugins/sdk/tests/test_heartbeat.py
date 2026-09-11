# @feature: FP-0.2.七 插件监控与指标 | @ci: python-coverage
"""sidecar 事件循环心跳测试。

契约（内核三信号假死判定的信号源）：
- run() 期间心跳协程周期向 stderr 写 `#HB <unix_millis>` 行（默认 10s，可
  经 AGENTOS_SIDECAR_HEARTBEAT_SECS 配置；"0" = 关闭）。
- 心跳跑在主事件循环内：事件循环冻结心跳即停（信号语义本身）。

SDK 侧与内核 client.rs::try_parse_heartbeat 跨语言，行为测试需起真实
sidecar 进程（属 e2e 车道）；此处以源码契约锚定漂移面：从 plugin.py 源码
提取心跳写行语句的模板，对插值表达式真实求值并断言其量纲与可解析性
（提取失败/表达式漂移/行帧丢失即红），不写与实现无关的自构造恒真检查。
"""

from __future__ import annotations

import re
import time as _time

import pytest

pytestmark = pytest.mark.unit


def _plugin_src() -> str:
    from agentos_plugin_sdk import plugin as plugin_mod

    path = plugin_mod.__file__
    assert path, "plugin.py 源码必须存在（模块已装载）"
    with open(path, encoding="utf-8") as f:
        return f.read()


def test_heartbeat_disabled_with_zero_interval() -> None:
    """AGENTOS_SIDECAR_HEARTBEAT_SECS=0 → 心跳协程直接返回（关闭契约）。"""
    src = _plugin_src()
    # 默认间隔 "10" 是契约值（内核三信号判定的心跳窗口按此标定）。
    assert 'os.environ.get("AGENTOS_SIDECAR_HEARTBEAT_SECS", "10")' in src
    # 关闭分支：interval <= 0 → 立即 return（缩进不敏感的结构匹配，
    # 对纯重排不误报；删分支/改比较方向即红）。
    assert re.search(r"if\s+interval\s*<=\s*0\s*:\s*return", src), (
        "心跳关闭契约漂移——AGENTOS_SIDECAR_HEARTBEAT_SECS=0 必须关闭心跳协程"
    )


def test_heartbeat_line_format_contract() -> None:
    """#HB 行格式契约：`#HB <unix_millis>\\n`（内核按行读 stderr 后解析毫秒）。"""
    src = _plugin_src()
    m = re.search(r'sys\.stderr\.write\(f"(#HB \{([^}]+)\})\\n"\)', src)
    assert m, "心跳写行语句漂移——内核 client.rs::try_parse_heartbeat 依赖 #HB <毫秒>\\n 行契约"
    template, expr = m.group(1), m.group(2)
    # 插值表达式真实求值：必须产出当前 unix 毫秒（时钟源 _time.time() ×
    # 1000 量纲换算），偏差超 5s 即视为量纲/时钟源漂移。
    value = eval(expr, {"int": int, "_time": _time})  # noqa: S307 —— 表达式取自本仓被测源码
    assert isinstance(value, int), f"心跳插值须产出整型毫秒，实得 {type(value).__name__}"
    assert abs(value - _time.time() * 1000) < 5_000, (
        f"心跳插值须为当前 unix 毫秒，实得 {value!r}"
    )
    # 行帧与解析面性质：模板固定前缀 "#HB "、以 \n 结尾（内核按行读）；
    # 实例化后的行整体匹配内核解析面 `#HB <纯数字>`。
    assert template == f"#HB {{{expr}}}", f"#HB 前缀漂移：{template!r}"
    line = template.replace(f"{{{expr}}}", str(value)) + "\n"
    assert re.fullmatch(r"#HB \d+\n", line), f"#HB 实例行不符内核解析面：{line!r}"
