# @feature: FP-0.2.二 内部模块 manifest | @ci: python-coverage
"""动态转发调用的容器凭据注入锁（复验轮 MF-R3）。

**缺陷形态**：隔离决策面已按目标工具名展开（`_effective_isolation_tool`），但注入面
仍按壳名匹配 → `docker_tool_names` 里是目标名 `bash`、而 `tc.name` 是 `call_tool`
→ 不命中 → `_container_id` 不注入 → 目标 bash 拿不到容器凭据、在宿主裸跑。

**第二层陷阱**：即使按壳名命中，注入键落**外层** args 也到不了目标——`call_tool`
只透传内层 `arguments`（注入键不进目标参数是既有断言）。故注入必须写**内层**。

本文件锁这两条：动态转发目标进容器时，`_container_id` 必须出现在内层 arguments 里，
且非动态调用行为零变化。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_THIS_DIR = Path(__file__).resolve().parent
_SHARED_DIR = _THIS_DIR.parents[0]
for _d in (str(_SHARED_DIR / "system"), str(_SHARED_DIR), str(_THIS_DIR)):
    if _d in sys.path:
        sys.path.remove(_d)
    sys.path.insert(0, _d)
for _bare in ("plugin", "decider"):
    sys.modules.pop(_bare, None)

_spec = importlib.util.spec_from_file_location(
    "isolation_guard_plugin_dyn_inject", str(_THIS_DIR / "plugin.py")
)
assert _spec is not None
assert _spec.loader is not None
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
IsolationGuard = _mod.IsolationGuard

_CID = "cid-abc123"


def _forward(target: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"id": "c1", "name": "call_tool", "args": {"tool": target, "arguments": arguments or {}}}


class TestDynamicForwardInjection:
    def test_forwarded_container_tool_gets_container_id_in_inner_args(self) -> None:
        """目标 bash 判进容器 → `_container_id` 落在**内层** arguments（可被目标读到）。"""
        calls = self._inject(_forward("bash", {"command": "ls"}), {"bash"})
        assert calls is not None, "动态转发目标进容器必须产生注入"
        args = calls[0]["args"]
        assert args["tool"] == "bash", "外层转发结构不变"
        assert args["arguments"]["_container_id"] == _CID, "容器凭据必须进内层目标参数"
        assert args["arguments"]["command"] == "ls", "目标原参数保留"
        assert args["arguments"]["working_dir"] == "/workspace", "bash 专属缺省挂载点"

    def test_forwarded_non_container_tool_not_injected(self) -> None:
        """目标不在判进容器的集合内 → 不注入（不误伤）。"""
        assert self._inject(_forward("file_read", {"path": "a"}), {"bash"}) is None

    def test_forwarded_injection_keeps_outer_keys_clean(self) -> None:
        """注入键不得出现在外层 args（否则 param_inject/透传语义会被扰乱）。"""
        calls = self._inject(_forward("bash", {"command": "ls"}), {"bash"})
        assert calls is not None
        assert "_container_id" not in calls[0]["args"]

    def test_direct_call_behavior_unchanged(self) -> None:
        """非动态调用（直接 bash 条目）行为零变化：容器键落该条目 args。"""
        calls = self._inject({"id": "c2", "name": "bash", "args": {"command": "ls"}}, {"bash"})
        assert calls is not None
        assert calls[0]["args"]["_container_id"] == _CID
        assert calls[0]["args"]["working_dir"] == "/workspace"

    def test_forwarded_exec_backend_injected_when_capable(self) -> None:
        """`_exec_backend` 与容器键同层注入（声明 capable 的目标才注入）。"""
        calls = self._inject(
            _forward("bash", {"command": "ls"}),
            {"bash"},
            exec_backend={"kind": "bwrap"},
            capable={"bash"},
        )
        assert calls is not None
        inner = calls[0]["args"]["arguments"]
        assert inner["_exec_backend"] == {"kind": "bwrap"}

    def test_forwarded_json_string_outer_args_injected(self) -> None:
        """外层 args 是 JSON **字符串**形态时同样注入（解析后写回 dict 内层）。

        `tool_core` 对部分来源的调用会把 `args` 传成 JSON 字符串，注入面若只认 dict
        就会静默漏注入——目标 bash 拿不到容器凭据而裸跑（与 MF-R3 同后果，形态不同）。
        """
        tc = {
            "id": "c3",
            "name": "call_tool",
            "args": json.dumps({"tool": "bash", "arguments": {"command": "ls"}}),
        }
        calls = self._inject(tc, {"bash"})
        assert calls is not None, "字符串外层同样必须产生注入"
        args = calls[0]["args"]
        assert isinstance(args, dict), "字符串外层应被解析为 dict 结构"
        assert args["tool"] == "bash"
        assert args["arguments"]["_container_id"] == _CID
        assert args["arguments"]["command"] == "ls", "目标原参数保留"

    def test_mixed_batch_forwarded_and_direct_both_injected(self) -> None:
        """混合批次：动态转发目标与直接容器工具在同一批里 → 各自按自身形态注入。

        两条注入路径（内层改写 / 外层直写）必须能在同一次调用里共存，否则「同一轮
        既有直调 bash 又有 call_tool(bash)」时其中一条会静默失去容器凭据。
        """
        calls = self._inject(
            [_forward("bash", {"command": "ls"}), {"id": "c4", "name": "bash", "args": {}}][0],
            {"bash"},
        )
        assert calls is not None
        assert calls[0]["args"]["arguments"]["_container_id"] == _CID

        # 真·混合批次：一次调用里两条不同形态的条目
        batch = IsolationGuard._inject_container_id(
            [
                _forward("bash", {"command": "ls"}),
                {"id": "c4", "name": "bash", "args": {"command": "pwd"}},
            ],
            {"bash"},
            _CID,
            exec_backend=None,
            exec_backend_capable=set(),
        )
        assert batch is not None
        assert len(batch) == 2
        # 第 1 条：转发壳 → 内层注入、外层保持干净
        assert batch[0]["name"] == "call_tool"
        assert batch[0]["args"]["arguments"]["_container_id"] == _CID
        assert "_container_id" not in batch[0]["args"]
        # 第 2 条：直调 → 外层注入（原有语义不变）
        assert batch[1]["name"] == "bash"
        assert batch[1]["args"]["_container_id"] == _CID

    @staticmethod
    def _inject(
        tc: dict[str, Any],
        docker_tool_names: set[str],
        *,
        exec_backend: dict[str, Any] | None = None,
        capable: set[str] | None = None,
    ) -> list[dict[str, Any]] | None:
        return IsolationGuard._inject_container_id(
            [tc],
            docker_tool_names,
            _CID,
            exec_backend=exec_backend,
            exec_backend_capable=capable or set(),
        )
