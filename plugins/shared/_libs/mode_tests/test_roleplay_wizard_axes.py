# @feature: FP-0.2.〇 RP物料进厂C2整合刀 | @vision: V1 可进化 | @ci: python-coverage
"""开演向导四轴候选集（C2 整合刀 2026-10-13）：候选端点 + 面板选择面 + 快照契约。

- 服务端 GET /data/wizard/axes：文风轴动态列包内 styles/（档位名+特征清单首行
  做选项描述；目录缺失=空列表降级不炸，损坏文件跳过）；尺度/节奏/代理权三
  静态轴（考据注释见 server.py 轴定义处）。
- 面板：「角色配置」步四轴单选组（radio 同名分组=单选互斥；数据驱动刀
  2026-10-13 自终步前移——选完卡立刻见到，终步只留开场白），缺省=尺度
  fade_to_black / 节奏 reasonable / 代理权 you_control / 文风 不选=无文风注入。
- 快照落值：四轴选择组装为 extra_context.rp_axes（k=v 逗号串，走既有 B8 通道
  零新透传面；注入消费=后续刀，契约登记在 material.py）。

用户空间隔离走 AGENTOS_USER_CONFIG_DIR（wizard panel 测试同款真实解析链）。
"""
from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

MODES_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_EP = "/ext/mode_roleplay/data"
_PANEL_HTML = Path(MODES_DIR) / "mode_roleplay" / "webview" / "roleplay_panel.html"

pytestmark = pytest.mark.unit


def _load_server() -> Any:
    """按唯一模块名装载 mode_roleplay server.py（防与其它模式测试模块互覆）。"""
    path = os.path.join(MODES_DIR, "mode_roleplay", "server.py")
    spec = importlib.util.spec_from_file_location("mode_axes_mode_roleplay", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def rp(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """server 模块 + 用户空间隔离（AGENTOS_USER_CONFIG_DIR → tmp 解析链）。"""
    user_config = tmp_path / "user_config"
    monkeypatch.setenv("AGENTOS_USER_CONFIG_DIR", str(user_config))
    module = _load_server()
    module.reset_providers()
    module.user_config = user_config
    return module


def _call(module: Any, path: str, method: str = "GET", raw_body: str = "") -> dict[str, Any]:
    return asyncio.run(module.http_handle(path=path, method=method, raw_body=raw_body))


def _body_json(result: dict[str, Any]) -> dict[str, Any]:
    assert result["success"] is True
    data = result["data"]
    assert data["body_encoding"] == "base64"
    return json.loads(base64.b64decode(data["body"]).decode("utf-8"))


# ── 候选集端点：文风轴动态列 styles/ ──────────────────────────────────────────


def _axes_payload(rp: Any) -> list[dict[str, Any]]:
    return _body_json(_call(rp, f"{_EP}/wizard/axes"))["axes"]


def test_axes_endpoint_shape_and_style_axis_dynamic(rp: Any) -> None:
    """候选集四轴有序；文风轴选项 = styles/ 目录 yaml 档（id/档位名/特征清单
    首行），文件增删自动跟随——非硬编码清单。"""
    axes = _axes_payload(rp)
    assert [a["id"] for a in axes] == ["style", "scale", "pace", "agency"]

    expected: dict[str, dict[str, str]] = {}
    for path in sorted(Path(rp._STYLES_DIR).glob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        expected[str(data["id"])] = {
            "name": str(data["name"]),
            "description": str(data["traits"][0]),
        }
    options = {o["id"]: o for o in axes[0]["options"]}
    assert set(options) == set(expected), "文风轴选项随 styles/ 目录动态"
    for style_id, meta in expected.items():
        assert options[style_id]["name"] == meta["name"]
        assert options[style_id]["description"] == meta["description"], "描述=特征清单首行"


def test_axes_endpoint_style_axis_options_carry_display_fields(rp: Any) -> None:
    """文风轴每个选项三字段齐（id/name/description），缺一不可（渲染面契约）。"""
    style = _axes_payload(rp)[0]
    assert style["options"], "出厂四档在场"
    for opt in style["options"]:
        assert set(opt) == {"id", "name", "description"}
        assert opt["name"]
        assert opt["description"]


# ── 候选集端点：三静态轴（考据清单）───────────────────────────────────────────

_STATIC_AXIS_OPTIONS: dict[str, set[str]] = {
    "scale": {"explicit", "implication", "fade_to_black"},
    "pace": {"reasonable", "butterfly", "normal"},
    "agency": {"you_control", "director", "input_improver"},
}


@pytest.mark.parametrize("axis_id", sorted(_STATIC_AXIS_OPTIONS))
def test_axes_endpoint_static_axis_options(rp: Any, axis_id: str) -> None:
    """三静态轴档集锁定（尺度 explicit/暗示/黑幕；节奏 合理/蝴蝶/普通；代理权
    You-control/Director/Input Improver），每档带展示名与说明。"""
    axis = next(a for a in _axes_payload(rp) if a["id"] == axis_id)
    assert {o["id"] for o in axis["options"]} == _STATIC_AXIS_OPTIONS[axis_id]
    for opt in axis["options"]:
        assert opt["name"], "静态档须带展示名"
        assert opt["description"], "静态档须带说明"


@pytest.mark.parametrize("axis_id", ["scale", "pace", "agency"])
def test_axes_endpoint_static_axes_have_labels(rp: Any, axis_id: str) -> None:
    """静态轴带展示轴名（面板分组标题消费）。"""
    axis = next(a for a in _axes_payload(rp) if a["id"] == axis_id)
    assert axis.get("label")


# ── 候选集端点：fail-soft ─────────────────────────────────────────────────────


def test_axes_endpoint_styles_dir_missing_degrades_empty(rp: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """styles/ 目录缺失 = 文风轴空列表不炸；静态三轴不受影响。"""
    monkeypatch.setattr(
        rp, "_STYLES_DIR", str(Path(rp._STYLES_DIR).parent / "no_such_styles_dir")
    )
    axes = _axes_payload(rp)
    style = next(a for a in axes if a["id"] == "style")
    assert style["options"] == [], "目录缺失 = 文风轴空列表（无文风注入常态）"
    scale = next(a for a in axes if a["id"] == "scale")
    assert {o["id"] for o in scale["options"]} == _STATIC_AXIS_OPTIONS["scale"]


def test_axes_endpoint_skips_corrupt_style_file(
    rp: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """单档损坏/非映射 = 该档跳过不崩面板（与 packs 扫描同款容错口径）。"""
    styles_copy = tmp_path / "styles"
    shutil.copytree(rp._STYLES_DIR, styles_copy)
    (styles_copy / "webnovel_paced.yaml").write_text("id: [截断\n", encoding="utf-8")
    (styles_copy / "non_mapping.yaml").write_text("- a\n- b\n", encoding="utf-8")
    monkeypatch.setattr(rp, "_STYLES_DIR", str(styles_copy))

    options = _axes_payload(rp)[0]["options"]
    ids = {o["id"] for o in options}
    assert "webnovel_paced" not in ids, "损坏 yaml 档跳过"
    assert "non_mapping" not in ids, "非映射 yaml 档跳过"
    assert "concrete_plain" in ids, "健康档不受损坏件影响"


# ── 面板：四轴 UI 面收敛（刀4「工作区玩法页全 webview 化」改钉，台账归因）──────
# 原四轴选择 UI（角色配置步单选组/rp_axes 快照生产）随纯玩法 setup 段收敛出面
# 板；候选集端点与概览计数消费保留（下一测试钉）。


def _panel_source() -> str:
    return _PANEL_HTML.read_text(encoding="utf-8")


def test_panel_axes_ui_retired_with_setup_section() -> None:
    """刀4 改钉（台账归因）：四轴选择 UI 与 rp_axes 快照生产随纯玩法 setup 段
    收敛出面板——init_ui 渲染与开演提交在 webview 页内（rp-core 单源装配），
    面板不保留退役渲染件（防死代码复活路径）。rp_axes 消费原为后置未实开
    （material.py 契约注释），生产退役=契约面退役，复活触发登记台账刀4 节。"""
    src = _panel_source()
    assert "wizStepAxesHtml" not in src
    assert "wizAxesSnapshot" not in src
    assert "wizAxisPick" not in src
    assert "extra.rp_axes" not in src, "面板不再生产 rp_axes 快照"


def test_panel_axes_candidates_endpoint_wired() -> None:
    """退役改钉（2026-10-14 用户裁定：概览计数行退役）：loadAxes 唯一消费面
    随概览计数行拔除——面板不再消费 /data/wizard/axes（服务端候选端点保留）。"""
    src = _panel_source()
    assert "function loadAxes()" not in src
    assert "state.errors.axes" not in src
    assert (
        "Promise.all([loadBootstrap(), loadCards(), loadLorebooks(), loadSessions(), "
        "loadPersonas(), loadDrafts(), loadPacks()])"
    ) in src
