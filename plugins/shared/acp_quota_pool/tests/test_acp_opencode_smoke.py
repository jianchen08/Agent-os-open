# @feature: FP-0.2.ACP额度池 M0 | @ci: python-coverage
"""OpenCode 原生 ACP 真端到端冒烟（M0 验收车道）。

本机装有 opencode（`opencode acp` 子命令，2026-10-15 实证 1.18.18）时跑
真实派发：引擎 spawn opencode → ACP 握手 → 新会话 → prompt → 收文本。
环境不可用（CI/未装机）则 skip 登记——禁全 mock 替代本车道。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from acp_engine import DispatchRequest, dispatch
from acp_ledger import QuotaLedger
from acp_registry import ProviderSpec

_OPENCODE = shutil.which("opencode")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        _OPENCODE is None,
        reason="本机无 opencode CLI——真端到端冒烟仅装机环境跑（CI skip 登记）",
    ),
]


async def test_opencode_real_acp_dispatch_roundtrip(tmp_path: Path) -> None:
    assert _OPENCODE is not None  # skipif 已守；窄化类型
    spec = ProviderSpec(
        id="opencode",
        name="OpenCode",
        command=_OPENCODE,
        args=("acp",),
        timeout_seconds=180.0,
        quota_model="Zen 免费模型档",
        cost_class="free",
        strength="unverified",
    )
    ledger = QuotaLedger(tmp_path / "ledger.json")

    result = await dispatch(
        DispatchRequest(
            goal="Reply with exactly PONG and nothing else. Do not read or modify any files.",
            cwd=str(tmp_path),
        ),
        spec,
        ledger,
    )

    assert result["status"] == "ok", f"opencode 派发失败: {result['summary']}"
    assert result["session_id"]
    assert "PONG" in result["summary"].upper()
    assert ledger.report()[0]["ok_total"] == 1
