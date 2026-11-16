# @feature: FP-0.2.〇 RP面板刀-Ⅰ 开演向导 | @vision: V1 可进化 | @ci: python-coverage
"""开演向导面板刀数据面 + webview 轻量断言（RP 面板刀-Ⅰ，FR-A3/FR-G4/J2/AC-2）。

- bootstrap 聚合玩法包清点（零新增端点）：包内 packs/ ∪ 用户层
  <user_config_dir>/packs 双根合并展示面（id/origin/计数），同 id 用户接管；
- 卡 pack_ids 随读面透传（向导玩法预选数据源）+ save 写面不丢（面板编辑
  round-trip 保真）；
- webview 向导面轻量断言：五步结构、透传契约键、入口按钮在场；内联脚本
  node --check（webview 纯 HTML 无构建线的语法自检，node 缺失 = 跳过）。

用户空间隔离走 AGENTOS_USER_CONFIG_DIR（server maturity 测试同款真实解析链）。
"""
from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest
import yaml

MODES_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_EP = "/ext/mode_roleplay/data"
_PANEL_HTML = (
    Path(MODES_DIR) / "mode_roleplay" / "webview" / "roleplay_panel.html"
)

pytestmark = pytest.mark.unit


def _load_server() -> Any:
    """按唯一模块名装载 mode_roleplay server.py（防与其它模式测试模块互覆）。"""
    path = os.path.join(MODES_DIR, "mode_roleplay", "server.py")
    spec = importlib.util.spec_from_file_location("mode_wizard_mode_roleplay", path)
    assert spec is not None and spec.loader is not None
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


def _post(module: Any, suffix: str, body: dict[str, Any]) -> dict[str, Any]:
    return _call(module, f"{_EP}{suffix}", method="POST", raw_body=json.dumps(body))


# ── bootstrap 玩法包清点（零新增端点）─────────────────────────────────────────


def test_bootstrap_carries_factory_packs_when_no_user_packs(rp: Any) -> None:
    """出厂播种两件玩法包（C3 整合刀）+ 用户层无 packs 目录 = 出厂两包在列
    （元数据键照常在场；origin=builtin）。"""
    payload = _body_json(_call(rp, f"{_EP}/bootstrap"))

    assert payload["mode"] == "roleplay"
    by_id = {p["id"]: p for p in payload["packs"]}
    assert set(by_id) == {"xianxia_combat", "stardew_social"}
    assert all(p["origin"] == "builtin" for p in by_id.values())


def test_bootstrap_lists_user_packs_with_counts(rp: Any) -> None:
    """用户层 packs：清点面透出 id/origin/计数；损坏/非白名单 id 文件跳过。"""
    packs = rp.user_config / "packs"
    packs.mkdir(parents=True)
    (packs / "pack_combat.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "战斗规则",
                "description": "回合制战斗",
                "variables": {"hp": {"type": "int"}, "mp": {"type": "int"}},
                "behaviors": {"怒击": {"trigger": "怒", "effect": {"hp": "-1"}}},
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    (packs / "pack_bare.yaml").write_text("variables: {}\n", encoding="utf-8")
    (packs / "坏名字.yaml").write_text("variables: {x: {type: int}}\n", encoding="utf-8")
    (packs / "pack_broken.yaml").write_text("variables: [截断\n", encoding="utf-8")

    payload = _body_json(_call(rp, f"{_EP}/bootstrap"))
    by_id = {p["id"]: p for p in payload["packs"]}
    by_id.pop("xianxia_combat", None)  # 出厂播种两件（C3）不在用户层断言域
    by_id.pop("stardew_social", None)

    assert set(by_id) == {"pack_combat", "pack_bare"}, "非白名单/损坏文件跳过"
    combat = by_id["pack_combat"]
    assert combat["origin"] == "user"
    assert combat["name"] == "战斗规则" and combat["description"] == "回合制战斗"
    assert combat["variables_count"] == 2 and combat["behaviors_count"] == 1
    assert by_id["pack_bare"]["variables_count"] == 0
    assert by_id["pack_bare"]["name"] == "pack_bare", "name 缺省回落 id"


def test_packs_listing_dual_root_takeover(rp: Any, tmp_path: Any) -> None:
    """双根合并：同 id 用户层接管（origin="user"），出厂件不在列示中出现。"""
    builtin = tmp_path / "builtin_packs"
    builtin.mkdir()
    (builtin / "pack_x.yaml").write_text(
        yaml.safe_dump({"name": "出厂版", "variables": {"a": {"type": "int"}}}),
        encoding="utf-8",
    )
    (builtin / "pack_only_builtin.yaml").write_text(
        yaml.safe_dump({"name": "仅出厂"}), encoding="utf-8"
    )
    user = rp.user_config / "packs"
    user.mkdir(parents=True)
    (user / "pack_x.yaml").write_text(
        yaml.safe_dump({"name": "用户接管版", "behaviors": {"k": {"trigger": "t", "effect": {}}}}),
        encoding="utf-8",
    )

    listing = rp._load_packs_listing(packs_dir=str(builtin), user_dir=str(user))
    by_id = {p["id"]: p for p in listing}

    assert set(by_id) == {"pack_x", "pack_only_builtin"}
    assert by_id["pack_x"]["origin"] == "user" and by_id["pack_x"]["name"] == "用户接管版"
    assert by_id["pack_x"]["variables_count"] == 0
    assert by_id["pack_x"]["behaviors_count"] == 1
    assert by_id["pack_only_builtin"]["origin"] == "builtin"


# ── 卡 pack_ids 透传（向导预选 + 编辑 round-trip 保真）────────────────────────


def test_card_payload_carries_pack_ids(rp: Any) -> None:
    """卡读面透传 pack_ids（向导玩法预选数据源）；缺省补空表。"""
    user_cards = rp.user_config / "agents" / "mode_roleplay"
    user_cards.mkdir(parents=True)
    (user_cards / "card_rp.yaml").write_text(
        yaml.safe_dump(
            {"name": "带包卡", "description": "d", "pack_ids": ["pack_a", "pack_b"]}
        ),
        encoding="utf-8",
    )

    cards = _body_json(_call(rp, f"{_EP}/cards"))["cards"]
    card = next(c for c in cards if c["id"] == "card_rp")
    builtin = next(c for c in cards if c["id"] == "card_luna")

    assert card["pack_ids"] == ["pack_a", "pack_b"]
    assert builtin["pack_ids"] == [], "出厂卡未声明 = 空表（非 list 一律 []）"


def test_card_save_roundtrips_pack_ids(rp: Any) -> None:
    """save 写面携带 pack_ids：面板编辑不再丢玩法包引用（round-trip 保真）。"""
    body = _body_json(
        _post(
            rp,
            "/cards/save",
            {"name": "回环卡", "description": "d", "pack_ids": ["pack_x"]},
        )
    )
    card_id = body["card_id"]
    stored = yaml.safe_load(
        (rp.user_config / "agents" / "mode_roleplay" / f"{card_id}.yaml").read_text(
            encoding="utf-8"
        )
    )
    assert stored["pack_ids"] == ["pack_x"]

    cards = _body_json(_call(rp, f"{_EP}/cards"))["cards"]
    assert next(c for c in cards if c["id"] == card_id)["pack_ids"] == ["pack_x"]


# ── webview 向导面轻量断言（宿主前端零改动，断言口全在插件 HTML）───────────────


def _panel_source() -> str:
    return _PANEL_HTML.read_text(encoding="utf-8")


def test_wizard_entry_and_five_steps_present() -> None:
    """v3.1 面板重构改钉（刀2 R2）+ 刀4 再改钉（台账归因）：五步能力映射为组合
    builder（①选卡画廊 ②世界观 chips ③初始状态自由文本 + 玩法包直挂）；
    init_ui 路线/词条选择件随纯玩法 setup 段收敛迁 webview 页内（消费锚见
    test_roleplay_pack_declarations 出厂页测试 test_factory_page_consumes_
    existing_payloads_only）；卡详情开场白选择保留（快捷开演路径，无向导
    testid）。锚点更新：builder 保存/开演双钮、card/book/pack 三类选项 testid
    与入口文案保全（data-id 委托不注入 onclick 汇点由下一测试执法）。"""
    src = _panel_source()
    assert 'data-testid="wiz-entry"' in src, "卡详情动作行有向导入口"
    assert "向导开演" in src
    assert 'data-testid="combo-save"' in src, "builder 存为自定义玩法按钮在场"
    assert 'data-testid="wiz-finish"' in src, "开演按钮在场（组合 builder）"
    assert 'data-testid="wiz-book-opt"' in src and 'data-testid="wiz-pack-opt"' in src
    assert 'data-testid="wiz-card-opt"' in src


def test_wizard_entry_id_not_interpolated_into_js_context() -> None:
    """向导入口卡 id 不拼进 onclick JS 字符串上下文（评审修复刀：HTML 实体转义
    在属性解码后还原引号可破 JS 字面量=转义汇点）；data-id 属性 + 事件委托
    读取，行为不变（点击开向导）。"""
    src = _panel_source()
    assert "onclick=\"openWizard" not in src, "openWizard 不得经 onclick 字符串拼接"
    assert "data-id=" in src, "入口 id 走 data-id 属性"
    assert "getAttribute('data-id')" in src, "点击委托自 data-id 读卡 id"


def test_wizard_passthrough_contract_keys_present() -> None:
    """透传契约键与桥调用在场（消费侧测试在 material / agent_config_load）。"""
    src = _panel_source()
    assert "wizard_lorebook_ids" in src
    assert "wizard_pack_ids" in src
    assert "wizard_profile" in src
    assert "roleplay_greeting" in src, "开场白与快捷开演同键同语义"
    assert "mode.session" in src, "向导开演走 mode.session 既有桥"
    assert "extra_context" in src
    assert "join(',')" in src, "列表按桥契约以逗号分隔 id 串透传"
    # 空选 = 键缺省（桥 fail-closed 只收非空字符串）：缺省守卫以 length 判空在场
    assert re.search(r"if \(w\.bookIds\.length\) extra\.wizard_lorebook_ids", src)
    assert re.search(r"if \(w\.packIds\.length\) extra\.wizard_pack_ids", src)


def test_panel_inline_script_syntax_valid() -> None:
    """内联脚本抽出版式后过 node --check（webview 纯 HTML 无构建线的语法自检）。"""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node 不可用（webview 无构建线，语法自检择机补跑）")
    src = _panel_source()
    match = re.search(r"<script>(.*)</script>", src, re.S)
    assert match is not None
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
        fh.write(match.group(1))
        path = fh.name
    try:
        proc = subprocess.run([node, "--check", path], capture_output=True, text=True)
    finally:
        os.unlink(path)
    assert proc.returncode == 0, f"node --check 失败: {proc.stderr}"
