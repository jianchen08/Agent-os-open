# @feature: FP-0.2.二 引导页面插件 | @ci: python-coverage
"""onboarding_service 内容装载校验 + 进度存储行为测试。

覆盖面：
1. 内容校验器（content_schema）：合法 walkthrough 通过；非法内容逐项拒载
   （完成条件类型/端点白名单/CTA 动作/wizard 键/id 唯一/复合条件非空）；
2. 出厂内容 lint：content/walkthroughs/*.json 全量过校验（防内容回归入 CI）；
3. 装载器：按 order 排序；default_open 全局唯一；坏文件错误上浮不静默；
4. 进度存储（progress_store）：缺文件空表、读写往返、合法合并（幂等性）、
   未知 id 拒绝、done=false 摘除、损坏文件显式报错。

mock 仅限文件系统（tmp_path 真实读写，不 mock）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

import content_schema  # noqa: E402
import progress_store  # noqa: E402

# ── 合法内容基线 ──────────────────────────────────────────────────────────────

def _valid_walkthrough() -> dict[str, Any]:
    return {
        "id": "get_started",
        "title": "开始使用",
        "description": "四步走通核心链",
        "order": 1,
        "default_open": True,
        "steps": [
            {"id": "welcome", "title": "欢迎", "body": "正文", "completion": {"type": "manual"}},
            {
                "id": "configure_llm",
                "title": "配置模型与 API",
                "body": "正文",
                "wizard": "llm_setup",
                "completion": {
                    "type": "all",
                    "conditions": [
                        {
                            "type": "api_check",
                            "endpoint": "/ext/llm_service/config/llm",
                            "json_path": "providers",
                            "op": "non_empty",
                        },
                        {
                            "type": "api_check",
                            "endpoint": "/ext/llm_service/config/llm",
                            "json_path": "defaults.chat",
                            "op": "non_empty",
                        },
                    ],
                },
            },
            {
                "id": "first_task",
                "title": "交给它第一个任务",
                "body": "正文",
                "cta": {"label": "打开任务管理", "action": {"type": "open_panel", "target": "/tasks"}},
                "completion": {"type": "panel_visited", "panel": "/tasks"},
            },
        ],
    }


# ── 校验器：合法通过 / 非法拒载 ────────────────────────────────────────────────

def test_valid_walkthrough_passes() -> None:
    errors = content_schema.validate_walkthrough(_valid_walkthrough())
    assert errors == []


def test_factory_content_all_valid() -> None:
    """出厂内容 lint：真实 content/walkthroughs 全量过校验（CI 防内容回归）。"""
    content_dir = _PLUGIN_DIR / "content" / "walkthroughs"
    files = sorted(content_dir.glob("*.json"))
    assert files, "出厂内容目录不应为空"
    walkthroughs, errors = content_schema.load_walkthroughs(content_dir)
    assert errors == []
    assert {w["id"] for w in walkthroughs} == {f.stem for f in files}


@pytest.mark.parametrize(
    ("mutate", "fragment"),
    [
        (lambda w: w.pop("id"), "id"),
        (lambda w: w.update({"id": ""}), "id"),
        (lambda w: w.update({"steps": []}), "steps"),
        (lambda w: w["steps"][1].update({"wizard": "no_such_wizard"}), "wizard"),
        (lambda w: w["steps"][0].update({"completion": {"type": "magic"}}), "完成条件类型"),
        (
            lambda w: w["steps"][0].update(
                {"completion": {"type": "api_check", "endpoint": "/api/v1/admin", "op": "non_empty"}}
            ),
            "endpoint",
        ),
        (
            lambda w: w["steps"][0].update(
                {
                    "completion": {
                        "type": "api_check",
                        "endpoint": "/ext/llm_service/config/llm",
                        "op": "non_empty",
                        "method": "POST",
                    }
                }
            ),
            "GET",
        ),
        (
            lambda w: w["steps"][0].update(
                {"completion": {"type": "all", "conditions": []}}
            ),
            "conditions",
        ),
        (
            lambda w: w["steps"][0].update(
                {"cta": {"label": "x", "action": {"type": "run_js", "target": "alert(1)"}}}
            ),
            "action",
        ),
        (
            lambda w: w["steps"][0].update(
                {"cta": {"label": "x", "action": {"type": "external_url", "target": "http://evil.com"}}}
            ),
            "https",
        ),
        (
            lambda w: w["steps"][0].update(
                {"cta": {"label": "x", "action": {"type": "open_panel", "target": "not-a-path"}}}
            ),
            "target",
        ),
        (
            lambda w: w["steps"].append(
                {**w["steps"][0], "id": "welcome", "title": "重复", "body": "x"}
            ),
            "重复",
        ),
    ],
    ids=[
        "missing_id",
        "empty_id",
        "empty_steps",
        "unknown_wizard",
        "unknown_completion_type",
        "api_check_endpoint_not_whitelisted",
        "api_check_method_must_be_get",
        "composite_conditions_empty",
        "cta_action_unknown_type",
        "external_url_must_be_https",
        "open_panel_target_must_be_path",
        "duplicate_step_id",
    ],
)
def test_invalid_walkthrough_rejected(mutate: Any, fragment: str) -> None:
    data = _valid_walkthrough()
    mutate(data)
    errors = content_schema.validate_walkthrough(data)
    assert errors, "非法内容必须产生校验错误"
    assert any(fragment in e for e in errors), f"错误信息应包含 {fragment!r}: {errors}"


def test_panel_visited_and_mode_selected_shapes() -> None:
    """非 api_check 条目的形状约束（≥2 组有区分度输入）。"""
    ok1 = content_schema.validate_walkthrough({
        "id": "w", "title": "t", "description": "d", "order": 1, "default_open": False,
        "steps": [{"id": "s", "title": "t", "body": "b",
                   "completion": {"type": "panel_visited", "panel": "/monitoring"}}],
    })
    ok2 = content_schema.validate_walkthrough({
        "id": "w2", "title": "t", "description": "d", "order": 2, "default_open": False,
        "steps": [{"id": "s", "title": "t", "body": "b",
                   "completion": {"type": "mode_selected", "mode": "mode_roleplay"}}],
    })
    assert ok1 == []
    assert ok2 == []
    bad = content_schema.validate_walkthrough({
        "id": "w3", "title": "t", "description": "d", "order": 3, "default_open": False,
        "steps": [{"id": "s", "title": "t", "body": "b",
                   "completion": {"type": "panel_visited", "panel": "tasks"}}],
    })
    assert any("panel" in e for e in bad)


# ── 装载器 ─────────────────────────────────────────────────────────────────────

def _write(tmp_path: Path, name: str, data: Any) -> None:
    d = tmp_path / "walkthroughs"
    d.mkdir(exist_ok=True)
    (d / name).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_loader_sorts_by_order(tmp_path: Path) -> None:
    lo = {**_valid_walkthrough(), "id": "a_low", "order": 2, "default_open": False}
    hi = {**_valid_walkthrough(), "id": "z_high", "order": 1, "default_open": False}
    _write(tmp_path, "a_low.json", lo)
    _write(tmp_path, "z_high.json", hi)
    walkthroughs, errors = content_schema.load_walkthroughs(tmp_path / "walkthroughs")
    assert errors == []
    assert [w["id"] for w in walkthroughs] == ["z_high", "a_low"]


def test_loader_default_open_must_be_unique(tmp_path: Path) -> None:
    _write(tmp_path, "a.json", {**_valid_walkthrough(), "id": "a"})
    _write(tmp_path, "b.json", {**_valid_walkthrough(), "id": "b"})
    walkthroughs, errors = content_schema.load_walkthroughs(tmp_path / "walkthroughs")
    assert walkthroughs == []
    assert errors
    assert any("default_open" in e["error"] for e in errors)


def test_loader_broken_file_surfaces_error(tmp_path: Path) -> None:
    d = tmp_path / "walkthroughs"
    d.mkdir()
    (d / "broken.json").write_text("{not json", encoding="utf-8")
    walkthroughs, errors = content_schema.load_walkthroughs(d)
    assert walkthroughs == []
    assert errors
    assert "broken.json" in errors[0]["file"]


# ── 进度存储 ───────────────────────────────────────────────────────────────────

_IDS = {"get_started": {"welcome", "configure_llm", "first_task"}, "task_management": {"meet"}}


def test_progress_missing_file_returns_empty(tmp_path: Path) -> None:
    assert progress_store.load_progress(tmp_path / "progress.json") == {}


def test_progress_roundtrip_and_merge_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "progress.json"
    p1, err = progress_store.apply_update(progress_store.load_progress(path), _IDS,
                                          {"walkthrough_id": "get_started", "step_id": "welcome",
                                           "done": True, "how": "manual"})
    assert err is None
    assert p1["get_started"]["welcome"]["done"] is True
    progress_store.save_progress(path, p1)
    loaded = progress_store.load_progress(path)
    # 幂等性质：同一更新重复应用，结果不变
    p2, _ = progress_store.apply_update(loaded, _IDS,
                                        {"walkthrough_id": "get_started", "step_id": "welcome",
                                         "done": True, "how": "manual"})
    assert p2 == loaded
    # 首次完成留痕：不同 how 的重复完成不改写历史（可审计）
    p3, _ = progress_store.apply_update(p2, _IDS,
                                        {"walkthrough_id": "get_started", "step_id": "welcome",
                                         "done": True, "how": "api_check"})
    assert p3 == p2


def test_progress_unknown_ids_rejected() -> None:
    for bad in (
        {"walkthrough_id": "nope", "step_id": "welcome", "done": True, "how": "manual"},
        {"walkthrough_id": "get_started", "step_id": "nope", "done": True, "how": "manual"},
    ):
        _, err = progress_store.apply_update({}, _IDS, bad)
        assert err is not None


def test_progress_undone_removes_entry(tmp_path: Path) -> None:
    _ = tmp_path  # 本用例纯字典语义，不落盘
    p1, _ = progress_store.apply_update({}, _IDS,
                                        {"walkthrough_id": "task_management", "step_id": "meet",
                                         "done": True, "how": "cta_clicked"})
    p2, err = progress_store.apply_update(p1, _IDS,
                                          {"walkthrough_id": "task_management", "step_id": "meet",
                                           "done": False, "how": "manual"})
    assert err is None
    assert p2 == {}


def test_progress_corrupt_file_raises(tmp_path: Path) -> None:
    path = tmp_path / "progress.json"
    path.write_text("{oops", encoding="utf-8")
    with pytest.raises(progress_store.ProgressCorruptError):
        progress_store.load_progress(path)
