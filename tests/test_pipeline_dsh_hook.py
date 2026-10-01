# @feature: FP-0.2.〇 管道引擎 | @vision: V3 可嵌入 | @ci: python-coverage
"""pipeline_dsh_hook 管道步骤契约（DSH 钩子翻译的执行端）。

锁四件事：
1. 正常执行：argv 子进程跑通，ok=True + exit_code=0 + event 透传 + 时长非负；
2. 失败可见：非零退出/超时/拉不起进程 → ok=False 携带证据（exit_code /
   stderr 尾巴 / 错误说明）；失败不抛异常不阻断管道——钩子是辅助步骤，
   失败必须可见（state + warning 日志）但不应炸轮次；
3. 配置面 fail-visible：inputs 缺失/形态错/timeout_ms 非法 → ok=False 带
   错误说明，绝不静默空跑；
4. 结果单点：全部结果落 state_updates["dsh_hook"]，键稳定可观测。
"""

from __future__ import annotations

import asyncio
import sys

import pytest

from tests._pipeline_plugin_path import add_plugin_dir

add_plugin_dir("hooks", "dsh_hook")
import plugin as dsh_hook_mod  # noqa: E402


def _run(inputs: dict, config: dict | None = None) -> dict:
    from pipeline.plugin import PluginContext

    plugin = dsh_hook_mod.DshHookPlugin()
    payload = config if config is not None else {"inputs": inputs}
    ctx = PluginContext(state={}, config=payload)
    result = asyncio.run(plugin.execute(ctx))
    return result.state_updates


class TestDshHookExecute:
    def test_plugin_identity(self):
        plugin = dsh_hook_mod.DshHookPlugin()
        assert plugin.name == "dsh_hook"
        assert isinstance(plugin.priority, int)

    def test_success(self):
        updates = _run({"event": "turn/start", "command": [sys.executable, "-c", "print('ok')"]})
        r = updates["dsh_hook"]
        assert r["ok"] is True
        assert r["exit_code"] == 0
        assert r["event"] == "turn/start"
        assert r["duration_ms"] >= 0

    def test_nonzero_exit_records_stderr(self):
        updates = _run({
            "event": "turn/end",
            "command": [sys.executable, "-c", "import sys; sys.stderr.write('boom-marker'); sys.exit(3)"],
        })
        r = updates["dsh_hook"]
        assert r["ok"] is False
        assert r["exit_code"] == 3
        assert "boom-marker" in r["stderr_tail"]

    def test_timeout_kills_promptly(self):
        updates = _run({
            "event": "turn/start",
            "command": [sys.executable, "-c", "import time; time.sleep(30)"],
            "timeout_ms": 300,
        })
        r = updates["dsh_hook"]
        assert r["ok"] is False
        assert "timeout" in r["error"].lower()
        # 及时返回而非等满 30s（真实子进程 + 真实短超时，无 mock）
        assert r["duration_ms"] < 10000

    def test_spawn_failure_visible(self):
        updates = _run({"command": ["definitely-missing-exec-xyz"]})
        r = updates["dsh_hook"]
        assert r["ok"] is False
        assert r["exit_code"] is None
        assert r["error"]

    @pytest.mark.parametrize(
        "inputs",
        [
            {},
            {"command": []},
            {"command": "node a.mjs"},
            {"command": ["node", 1]},
            {"command": [sys.executable, "-c", "print(1)"], "timeout_ms": 0},
            {"command": [sys.executable, "-c", "print(1)"], "timeout_ms": -5},
        ],
    )
    def test_config_errors_fail_visible(self, inputs: dict):
        updates = _run(inputs)
        r = updates["dsh_hook"]
        assert r["ok"] is False
        assert r["error"]

    def test_missing_inputs_key(self):
        updates = _run({}, config={})
        assert updates["dsh_hook"]["ok"] is False

    def test_cwd_passthrough(self, tmp_path):
        marker = tmp_path / "marker.txt"
        script = "import pathlib; pathlib.Path('marker.txt').write_text('x')"
        updates = _run({"command": [sys.executable, "-c", script], "cwd": str(tmp_path)})
        assert updates["dsh_hook"]["ok"] is True
        assert marker.exists()
