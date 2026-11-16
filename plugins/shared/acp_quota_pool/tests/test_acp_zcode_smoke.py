# @feature: FP-0.2.ACP额度池 M1 | @ci: python-coverage
"""ZCode one-shot 真端到端冒烟（M1 验收车道）。

本机装有 ZCode 桌面壳自带的 zcode.cjs（`-p` 无头入口，0.16.9 实证）与 node
运行时时跑真实派发：引擎 spawn `node zcode.cjs -p <goal> --json --mode plan`
→ 收 JSON 结果对象。`--mode plan` 只读 + goal 显式禁读写文件（防误伤）。
环境不可用（CI/未装机/无头模型未激活）则 skip 登记——禁全 mock 替代本车道。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from acp_engine import DispatchRequest, dispatch
from acp_ledger import QuotaLedger
from acp_registry import ProviderSpec

_ZCODE_CJS = Path(r"C:\Program Files\ZCode\resources\glm\zcode.cjs")
_NODE = shutil.which("node")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        _NODE is None or not _ZCODE_CJS.is_file(),
        reason="本机无 node 或 zcode.cjs——真端到端冒烟仅装机环境跑（CI skip 登记）",
    ),
]


async def test_zcode_real_oneshot_dispatch_roundtrip(tmp_path: Path) -> None:
    assert _NODE is not None  # skipif 已守；窄化类型
    spec = ProviderSpec(
        id="zcode",
        name="ZCode",
        command=_NODE,
        args=(str(_ZCODE_CJS), "-p", "{goal}", "--json", "--mode", "plan"),
        timeout_seconds=300.0,
        quota_model="GLM coding plan",
        cost_class="paid",
        strength="unverified",
        backend="oneshot",
    )
    ledger = QuotaLedger(tmp_path / "ledger.json")

    result = await dispatch(
        DispatchRequest(
            goal="Reply with exactly PONG and nothing else. Do not read or write any files.",
            cwd=str(tmp_path),
        ),
        spec,
        ledger,
    )

    assert result["status"] == "ok", f"zcode 派发失败: {result['summary']}"
    assert result["session_id"] == ""  # one-shot：无会话语义
    assert "PONG" in result["summary"].upper()
    row = ledger.report()[0]
    assert row["ok_total"] == 1
    assert row["last_session_id"] == ""
