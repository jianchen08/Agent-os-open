# @feature: FP-0.2.〇 RP面板数据驱动刀 | @vision: V1 可进化 | @ci: python-coverage
"""面板数据驱动呈现（2026-10-13）：后端有什么、面板显示什么，零硬编码清单。

- 服务端 GET /data/packs：玩法包双根清点独立端点（复用 _load_packs_listing
  清点机；出厂∪用户层同 id 接管；fail-soft 目录缺失=空列表）——向导玩法勾选
  组与概览计数的直接数据源（bootstrap 聚合键保留，载荷形状被既有测试钉死）。
- 面板四轴前移：单选组自终步（开场白）挪到「角色配置」步（选完卡立刻见到），
  终步只留开场白；loadAxes 数据流与端点不动。
- 面板玩法包数据源切到 /data/packs（loadPacks 入 refreshAll 装载链，失败≠空
  数据 errors.packs 留证）；勾选值沿既有 wizard_pack_ids 透传通道（消费在
  agent_config_load，透传契约由 test_roleplay_wizard_panel 钉死）。
- 概览能力计数行已随 v3.1 设计保真走查退役（用户裁定 2026-10-14：设计稿没有
  且空记录态渲染无意义）——退役改钉见 test_panel_overview_counts_retired；
  loadAxes 与 state.axes 随唯一消费面一同拔除。

用户空间隔离走 AGENTOS_USER_CONFIG_DIR（wizard axes 测试同款真实解析链）。
"""
from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

MODES_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_EP = "/ext/mode_roleplay/data"
_PACKS_ROUTE = f"{_EP}/packs"
_PANEL_HTML = Path(MODES_DIR) / "mode_roleplay" / "webview" / "roleplay_panel.html"
_MANIFEST = Path(MODES_DIR) / "mode_roleplay" / "plugin.json"

pytestmark = pytest.mark.unit


def _load_server() -> Any:
    """按唯一模块名装载 mode_roleplay server.py（防与其它模式测试模块互覆）。"""
    path = os.path.join(MODES_DIR, "mode_roleplay", "server.py")
    spec = importlib.util.spec_from_file_location("mode_datadriven_mode_roleplay", path)
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


# ── 服务端：GET /data/packs 双根清点端点 ──────────────────────────────────────


def test_packs_endpoint_lists_factory_packs_with_display_fields(rp: Any) -> None:
    """出厂两包在列（origin=builtin），载荷含展示字段 id/name/description/origin，
    name/description 与 packs/ 实际 yaml 同源——端点不硬编码清单。"""
    payload = _body_json(_call(rp, _PACKS_ROUTE))
    by_id = {p["id"]: p for p in payload["packs"]}
    assert by_id.keys() >= {"xianxia_combat", "stardew_social"}

    for path in sorted(Path(rp._PACKS_DIR).glob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        item = by_id[path.stem]
        assert item["origin"] == "builtin"
        assert item["name"] == str(data["name"]), "name 与包内 yaml 同源"
        assert item["description"] == str(data.get("description") or "")
    for item in by_id.values():
        assert {"id", "name", "description", "origin"} <= set(item)


def test_packs_endpoint_dual_root_user_takeover(rp: Any) -> None:
    """双根合并：用户层同 id 接管（origin=user），异 id 用户包并入列表。"""
    packs = rp.user_config / "packs"
    packs.mkdir(parents=True)
    (packs / "xianxia_combat.yaml").write_text(
        yaml.safe_dump({"name": "修仙战斗·用户版", "description": "接管件", "variables": {}}),
        encoding="utf-8",
    )
    (packs / "homebrew.yaml").write_text(
        yaml.safe_dump({"name": "自建包", "description": "用户层新增"}), encoding="utf-8"
    )

    by_id = {p["id"]: p for p in _body_json(_call(rp, _PACKS_ROUTE))["packs"]}
    assert by_id["xianxia_combat"]["origin"] == "user", "同 id 用户层接管"
    assert by_id["xianxia_combat"]["name"] == "修仙战斗·用户版"
    assert by_id["homebrew"]["origin"] == "user"
    assert by_id["stardew_social"]["origin"] == "builtin", "未被接管的出厂件照常在列"


def test_packs_endpoint_missing_dirs_degrade_empty(rp: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """fail-soft：出厂根缺失 + 用户层无 packs 目录 = 空列表不炸（读面降级）。"""
    monkeypatch.setattr(
        rp, "_PACKS_DIR", str(Path(rp._PACKS_DIR).parent / "no_such_packs_dir")
    )
    payload = _body_json(_call(rp, _PACKS_ROUTE))
    assert payload["packs"] == []


def test_packs_endpoint_registered_in_routes_and_manifest(rp: Any) -> None:
    """路由面双注册：server _DATA_ROUTES 读面集合 + plugin.json http_endpoints
    声明（GET + http.handle）——漏任一即 404 不可见。"""
    assert _PACKS_ROUTE in rp._DATA_ROUTES
    manifest = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    declared = [
        ep
        for ep in manifest["http_endpoints"]
        if ep["path"] == _PACKS_ROUTE
    ]
    assert len(declared) == 1, "plugin.json 恰一条 /data/packs 声明"
    assert declared[0]["method"] == "GET"
    assert declared[0]["handler_capability"] == "http.handle"


# ── 面板：玩法包数据源切独立端点（零硬编码包名）────────────────────────────────
# （原「四轴前移步位」测试随刀4 四轴 UI 面收敛删除——退役改钉在
# test_roleplay_wizard_axes.test_panel_axes_ui_retired_with_setup_section。）


def _panel_source() -> str:
    return _PANEL_HTML.read_text(encoding="utf-8")


def _panel_slice(src: str, start: str, end: str) -> str:
    return src.split(start, 1)[1].split(end, 1)[0]


def test_panel_packs_loaded_from_dedicated_endpoint() -> None:
    """loadPacks 消费 /data/packs 入 refreshAll 装载链；失败≠空数据
    （errors.packs 留证）；bootstrap 不再是 packs 来源（单源无漂移）。"""
    src = _panel_source()
    assert "function loadPacks()" in src
    assert "'/ext/mode_roleplay/data/packs'" in src
    assert (
        "Promise.all([loadBootstrap(), loadCards(), loadLorebooks(), loadSessions(), "
        "loadPersonas(), loadDrafts(), loadPacks()])"
    ) in src, "loadPacks 入 refreshAll 装载链"
    assert "state.errors.packs" in src
    assert "state.packs = d.packs" not in src, "bootstrap 不再承载 packs（单源=新端点）"


def test_panel_pack_group_zero_hardcoded_pack_names() -> None:
    """数据驱动铁律：面板全文零出厂包名字面量——后端有什么、面板显示什么。"""
    src = _panel_source()
    assert "xianxia_combat" not in src
    assert "stardew_social" not in src


def test_panel_pack_step_surfaces_load_error() -> None:
    """向导玩法步：清点读取失败如实上屏（对齐 axes 错误横幅先例），不静默空表。"""
    src = _panel_source()
    packs = _panel_slice(src, "function wizStepPacksHtml", "function comboFoldNote")
    assert "state.errors.packs" in packs, "玩法步消费 errors.packs"


# ── 面板：概览能力计数行退役（v3.1 设计保真，用户裁定 2026-10-14）──────────────


def test_panel_overview_counts_retired() -> None:
    """概览计数行退役改钉：设计稿首页没有该行，空记录态渲染「--」占位无意义——
    整行与 loadAxes（其唯一剩余消费面）一并拔除，大厅只渲染设计稿内容。"""
    src = _panel_source()
    assert "overview-counts" not in src
    assert "overviewCountsHtml" not in src
    assert "overviewCount(" not in src
    assert "后端能力" not in src
    assert "function loadAxes()" not in src
    assert "state.axes" not in src


def test_panel_overview_rerenders_after_refresh() -> None:
    """装载完成且大厅在场时重渲染大厅（卡网格随刷新落位，不滞留装载前空态）。"""
    src = _panel_source()
    assert 'id="overviewPanel"' in src, "大厅容器带 id（在场性探测锚）"
    assert "if (state.view === 'hall') showWelcome();" in src
