# @feature: FP-0.2.二 内部模块 manifest | @ci: python-coverage
"""community_ops FeedbackLedgerTool 单元测试。

覆盖 plugins/shared/tools/community_ops/tool.py 的 FeedbackLedgerTool：
1. get_tool_definition 工具契约（名称/必填/状态枚举/output schema）
2. 台账路径解析：参数 > 环境变量 AGENTOS_COMMUNITY_LEDGER > 默认相对路径
3. append：缺参拒绝 / 自动编号单调递增 / 完整字段落盘 / 父目录自动创建
4. update_status：缺参拒绝 / 条目不存在 / 状态推进与备注覆盖
5. list：全量 / filter_status 过滤 / limit 截断 / total 不受 limit 影响
6. 损坏防护：JSON 损坏报 LEDGER_CORRUPT（不静默重置）/ 结构非法报
   LEDGER_ERROR / 父路径为文件报 LEDGER_ERROR

外部依赖打桩：无网络依赖，文件系统用 pytest tmp_path 真实读写。
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))


def _load_tool() -> Any:
    """动态加载 tool.py（唯一模块名，避免与裸名 tool 冲突）。"""
    mod_name = "community_ops_ledger_test"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "tool.py")
    assert spec is not None, "Cannot load tool.py"
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


_MOD = _load_tool()
FeedbackLedgerTool = _MOD.FeedbackLedgerTool


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _append(tool: Any, path: Path, **overrides: Any) -> Any:
    inputs: dict[str, Any] = {
        "action": "append",
        "source": "github_issue",
        "summary": "安装报错",
        "ledger_path": str(path),
    }
    inputs.update(overrides)
    return _run(tool.execute(inputs))


def test_definition_contract() -> None:
    tool = FeedbackLedgerTool.get_tool_definition()
    assert tool.name == "feedback_ledger"
    assert tool.input_schema["required"] == ["action"]
    assert set(tool.input_schema["properties"]["action"]["enum"]) == {
        "append",
        "update_status",
        "list",
    }
    statuses = tool.input_schema["properties"]["status"]["enum"]
    assert "待处置" in statuses
    assert "已入 ROADMAP" in statuses
    assert "success" in tool.output_schema["required"]


def test_ledger_path_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AGENTOS_COMMUNITY_LEDGER", raising=False)
    tool = FeedbackLedgerTool()
    default = tool._ledger_path({})
    assert default == _MOD._DEFAULT_LEDGER
    monkeypatch.setenv("AGENTOS_COMMUNITY_LEDGER", "env-ledger.json")
    assert tool._ledger_path({}) == Path("env-ledger.json")
    assert tool._ledger_path({"ledger_path": "param.json"}) == Path("param.json")


def test_append_creates_and_numbers_monotonically(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "ledger.json"
    tool = FeedbackLedgerTool()
    result = _append(tool, path)
    assert result.success is True
    entry = result.output["entry"]
    assert entry["id"] == "FB-001"
    assert entry["status"] == "待处置"
    assert entry["source"] == "github_issue"
    assert path.exists()
    result = _append(tool, path, summary="第二条")
    assert result.output["entry"]["id"] == "FB-002"
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert [e["id"] for e in stored["entries"]] == ["FB-001", "FB-002"]


def test_append_requires_source_and_summary(tmp_path: Path) -> None:
    tool = FeedbackLedgerTool()
    path = tmp_path / "ledger.json"
    result = _run(
        tool.execute({"action": "append", "source": "web", "ledger_path": str(path)})
    )
    assert result.success is False
    assert result.error_code == "MISSING_FIELDS"
    result = _run(
        tool.execute({"action": "append", "summary": "x", "ledger_path": str(path)})
    )
    assert result.success is False
    assert result.error_code == "MISSING_FIELDS"


def test_append_with_full_fields(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    tool = FeedbackLedgerTool()
    result = _append(
        tool,
        path,
        category="feature",
        status="已回复",
        link="https://example.com/x",
    )
    entry = result.output["entry"]
    assert entry["category"] == "feature"
    assert entry["status"] == "已回复"
    assert entry["link"] == "https://example.com/x"


def test_update_status_advances_entry(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    tool = FeedbackLedgerTool()
    _append(tool, path)
    result = _run(
        tool.execute(
            {
                "action": "update_status",
                "id": "FB-001",
                "status": "已入候选池",
                "note": "待用户拍板",
                "ledger_path": str(path),
            }
        )
    )
    assert result.success is True
    assert result.output["entry"]["status"] == "已入候选池"
    assert result.output["entry"]["note"] == "待用户拍板"
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["entries"][0]["status"] == "已入候选池"


def test_update_status_gates(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    tool = FeedbackLedgerTool()
    result = _run(
        tool.execute({"action": "update_status", "id": "FB-001", "ledger_path": str(path)})
    )
    assert result.error_code == "MISSING_FIELDS"
    _append(tool, path)
    result = _run(
        tool.execute(
            {
                "action": "update_status",
                "id": "FB-999",
                "status": "已回复",
                "ledger_path": str(path),
            }
        )
    )
    assert result.success is False
    assert result.error_code == "ENTRY_NOT_FOUND"


def test_list_filter_and_limit(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    tool = FeedbackLedgerTool()
    _append(tool, path, summary="a")
    _append(tool, path, summary="b", status="已回复")
    _append(tool, path, summary="c")
    result = _run(tool.execute({"action": "list", "ledger_path": str(path)}))
    assert result.output["total"] == 3
    assert len(result.output["entries"]) == 3
    result = _run(
        tool.execute(
            {"action": "list", "filter_status": "已回复", "ledger_path": str(path)}
        )
    )
    assert result.output["total"] == 1
    assert result.output["entries"][0]["summary"] == "b"
    result = _run(
        tool.execute({"action": "list", "limit": 2, "ledger_path": str(path)})
    )
    assert len(result.output["entries"]) == 2
    assert result.output["total"] == 3


def test_invalid_action(tmp_path: Path) -> None:
    tool = FeedbackLedgerTool()
    result = _run(
        tool.execute({"action": "delete_all", "ledger_path": str(tmp_path / "l.json")})
    )
    assert result.success is False
    assert result.error_code == "INVALID_ACTION"


def test_corrupt_ledger_not_silently_reset(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    path.write_text("{not json", encoding="utf-8")
    tool = FeedbackLedgerTool()
    result = _append(tool, path)
    assert result.success is False
    assert result.error_code == "LEDGER_CORRUPT"


def test_invalid_structure_rejected(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    path.write_text(json.dumps({"foo": []}), encoding="utf-8")
    tool = FeedbackLedgerTool()
    result = _append(tool, path)
    assert result.success is False
    assert result.error_code == "LEDGER_ERROR"


def test_parent_is_file_reports_error(tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file", encoding="utf-8")
    path = blocker / "ledger.json"
    tool = FeedbackLedgerTool()
    result = _append(tool, path)
    assert result.success is False
    assert result.error_code == "LEDGER_ERROR"
