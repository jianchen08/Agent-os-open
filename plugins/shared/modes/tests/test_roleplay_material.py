# @feature: FP-0.2.二 模式体系测试补标 | @ci: python-coverage
"""角色扮演模式物料组装器（mode_roleplay/material.py::build_injection）单测。

- 端到端：出厂真实包（card_luna + demo_world）驱动，断言卡键路去重（接管
  声明/设定段不重复——context_build 已按卡键加载卡 yaml system_prompt）/
  对话示例替换/世界书关键词命中注入；
- 行为矩阵：tmp 构造卡与世界书（对话示例段/占位符替换/世界书命中与未命中/
  persona 旁路/降级路径），各行为 ≥2 组区分度输入；
- 降级契约：卡缺失/卡损坏 → 空串不抛出。
"""
from __future__ import annotations

import importlib.util
import os
import sys
from typing import Any

import pytest
import yaml

MODES_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROLEPLAY_DIR = os.path.join(MODES_DIR, "mode_roleplay")
LUNA_CARD = os.path.join(ROLEPLAY_DIR, "agents", "card_luna.yaml")

pytestmark = pytest.mark.unit


def _load_material_module():
    """按真实导入名（mode_roleplay.material）装载并注册 sys.modules（防双实例）。"""
    name = "mode_roleplay.material"
    if name in sys.modules:
        return sys.modules[name]
    path = os.path.join(ROLEPLAY_DIR, "material.py")
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


material = _load_material_module()


def _card_state(**extra: Any) -> dict[str, Any]:
    """卡键 state（fresh 开演形态：agent.id = 卡键）。"""
    state: dict[str, Any] = {"agent.id": "mode_roleplay/card_luna"}
    state.update(extra)
    return state


def _write_card(agents_dir, card_id: str, card: dict[str, Any]) -> None:
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / f"{card_id}.yaml").write_text(
        yaml.safe_dump(card, allow_unicode=True), encoding="utf-8"
    )


def _write_book(lorebooks_dir, book_id: str, book: dict[str, Any]) -> None:
    lorebooks_dir.mkdir(parents=True, exist_ok=True)
    (lorebooks_dir / f"{book_id}.yaml").write_text(
        yaml.safe_dump(book, allow_unicode=True), encoding="utf-8"
    )


_MIN_CARD: dict[str, Any] = {
    "name": "测试角色",
    "description": "测试用角色设定文本。",
    "personality": "冷静、好奇。",
    "scenario": "测试场景：一间空屋子。",
    "mes_example": "<START>\n{{user}}: 你好\n{{char}}: 你好啊。",
    "lorebook_ids": ["test_world"],
}

_MIN_BOOK: dict[str, Any] = {
    "name": "测试世界",
    "entries": [
        {
            "keys": ["空屋子"],
            "content": "空屋子里有一把旧椅子。",
            "enabled": True,
            "insertion_order": 100,
        }
    ],
}


class TestCardKeyAppendFromRealSeed:
    def test_real_card_key_no_takeover_duplication(self) -> None:
        """卡键路去重：接管声明/设定/性格/场景不在此重复注入（context_build 已
        按卡键加载卡 yaml system_prompt 自带同内容，重复即双份）。"""
        pkg_dir = ROLEPLAY_DIR

        text = material.build_injection(_card_state(), pkg_dir)

        assert "# 扮演接管" not in text
        assert "你将扮演" not in text
        assert "月光神殿的最后一位守望者" not in text, "卡 description 不再重复"
        assert "温柔、古雅" not in text, "卡 personality 不再重复"
        assert "暴雨夜" not in text, "卡 scenario 不再重复"

    def test_real_card_mes_example_placeholder_replaced(self) -> None:
        """对话示例解析：{{char}}/{{user}} 替换为卡名/中性称谓，无占位符残留。"""
        text = material.build_injection(_card_state(), ROLEPLAY_DIR)

        assert "# 对话示例" in text
        assert "塞拉菲娜·月语: " in text, "{{char}} 替换为卡名"
        assert "用户: 你会魔法吗？" in text, "{{user}} 替换为中性称谓"
        assert "{{char}}" not in text, "无 char 占位符残留"
        assert "{{user}}" not in text, "无 user 占位符残留"

    def test_real_card_lorebook_keyword_hit_injects(self) -> None:
        """真实世界书：最近消息命中「月光神殿」→ 世界设定块注入对应条目。"""
        state = _card_state(
            messages=[{"role": "user", "content": "月光神殿今晚还点灯吗？"}]
        )

        text = material.build_injection(state, ROLEPLAY_DIR)

        assert "## 世界设定" in text
        assert "现任守望者只有塞拉菲娜一人" in text, "命中条目 content 注入"

    def test_real_card_lorebook_miss_zero_injection(self) -> None:
        """真实世界书：消息未命中任何关键词 → 无世界设定块（零注入）。"""
        state = _card_state(messages=[{"role": "user", "content": "今天天气不错"}])

        text = material.build_injection(state, ROLEPLAY_DIR)

        assert "## 世界设定" not in text
        assert "# 对话示例" in text, "对话示例段不受世界书影响"


class TestCardKeyAppendFromTmpCard:
    @pytest.fixture
    def pkg(self, tmp_path):
        """tmp 假包：agents/ 与 lorebooks/ 目录坐标。"""
        return {
            "root": tmp_path,
            "agents": tmp_path / "agents",
            "lorebooks": tmp_path / "lorebooks",
        }

    def test_minimal_card_examples_only(self, pkg) -> None:
        """tmp 最小卡：只追加对话示例段（占位符替换对位）；设定/性格/场景与
        接管声明不重复注入（context_build 已按卡键加载 system_prompt）。"""
        _write_card(pkg["agents"], "card_min", _MIN_CARD)

        text = material.build_injection(
            {"agent.id": "mode_roleplay/card_min"}, pkg["root"]
        )

        assert "# 对话示例" in text
        assert "测试角色: 你好啊。" in text, "{{char}} 替换为卡名"
        assert "用户: 你好" in text, "{{user}} 替换为中性称谓"
        assert "# 扮演接管" not in text
        assert "你将扮演" not in text
        assert "设定：测试用角色设定文本。" not in text
        assert "# 性格" not in text
        assert "# 场景" not in text
        assert "{{char}}" not in text
        assert "{{user}}" not in text

    def test_card_without_optional_fields_zero_injection(self, pkg) -> None:
        """只有名字的卡：无示例无书 = 空串（不产出接管声明或空段）。"""
        _write_card(pkg["agents"], "card_bare", {"name": "素卡"})

        text = material.build_injection(
            {"agent.id": "mode_roleplay/card_bare"}, pkg["root"]
        )

        assert text == ""

    def test_lorebook_hit_and_miss(self, pkg) -> None:
        """世界书命中/未命中两组输入：命中注入条目，未命中零注入。"""
        _write_card(pkg["agents"], "card_min", _MIN_CARD)
        _write_book(pkg["lorebooks"], "test_world", _MIN_BOOK)
        hit = {"agent.id": "mode_roleplay/card_min",
               "messages": [{"role": "user", "content": "那间空屋子还有人住吗"}]}
        miss = {"agent.id": "mode_roleplay/card_min",
                "messages": [{"role": "user", "content": "今天吃什么"}]}

        hit_text = material.build_injection(hit, pkg["root"])
        miss_text = material.build_injection(miss, pkg["root"])

        assert "空屋子里有一把旧椅子。" in hit_text
        assert "## 世界设定" not in miss_text
        assert "# 对话示例" in miss_text

    def test_books_dir_overrides_package_lorebooks(self, pkg, tmp_path) -> None:
        """books_dir 参数：外部书目录优先生效（用户层书扩展点契约）。"""
        _write_card(pkg["agents"], "card_min", _MIN_CARD)
        external = tmp_path / "external_books"
        _write_book(external, "test_world", _MIN_BOOK)
        (pkg["lorebooks"]).mkdir(parents=True, exist_ok=True)
        # 包内同名书写入不同内容——命中断言只认外部书内容，证明确实切换了目录
        _write_book(pkg["lorebooks"], "test_world", {
            "entries": [{"keys": ["空屋子"], "content": "包内版本不该出现"}]
        })
        state = {"agent.id": "mode_roleplay/card_min",
                 "messages": [{"role": "user", "content": "空屋子"}]}

        text = material.build_injection(state, pkg["root"], books_dir=str(external))

        assert "空屋子里有一把旧椅子。" in text
        assert "包内版本不该出现" not in text

    def test_missing_book_file_is_zero_injection(self, pkg) -> None:
        """绑定的书文件缺失 = 零注入（不抛出）。"""
        card = dict(_MIN_CARD, lorebook_ids=["ghost_book"])
        _write_card(pkg["agents"], "card_min", card)
        state = {"agent.id": "mode_roleplay/card_min",
                 "messages": [{"role": "user", "content": "空屋子"}]}

        text = material.build_injection(state, pkg["root"])

        assert "## 世界设定" not in text
        assert "# 对话示例" in text

    def test_multimodal_content_text_blocks_scanned(self, pkg) -> None:
        """多模态 content（块列表）：text 块进扫描窗口（关键词命中）。"""
        _write_card(pkg["agents"], "card_min", _MIN_CARD)
        _write_book(pkg["lorebooks"], "test_world", _MIN_BOOK)
        state = {
            "agent.id": "mode_roleplay/card_min",
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": "看看这间空屋子"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,x"}},
                ],
            }],
        }

        text = material.build_injection(state, pkg["root"])

        assert "空屋子里有一把旧椅子。" in text


class TestPersonaBypassAndDegradation:
    @pytest.fixture
    def pkg(self, tmp_path):
        return tmp_path

    def test_persona_takeover_when_not_card_key(self, pkg) -> None:
        """非卡键 + roleplay_persona 非空（附身场景，context_build 无卡数据）：
        接管块全量必要——接管声明 + 人设文本。"""
        state = {
            "agent.id": "agentos",
            "execution_context": {"roleplay_persona": "一位沉默寡言的老船长。"},
        }

        text = material.build_injection(state, pkg)

        assert "# 扮演接管" in text
        assert "你将接管以下人设" in text
        assert "一位沉默寡言的老船长。" in text

    def test_no_persona_no_card_key_returns_empty(self, pkg) -> None:
        """非卡键且无 persona = 零注入空串（通用步骤不追加）。"""
        assert material.build_injection({"agent.id": "agentos"}, pkg) == ""
        assert material.build_injection({}, pkg) == ""

    def test_persona_empty_string_returns_empty(self, pkg) -> None:
        """persona 空串/纯空白 = 视同无 persona（零注入）。"""
        state = {"execution_context": {"roleplay_persona": "   "}}
        assert material.build_injection(state, pkg) == ""

    def test_card_key_takes_priority_over_persona(self, pkg) -> None:
        """卡键成立时 persona 分支不参与（卡优先，键语义不静默漂移）。"""
        _write_card(pkg / "agents", "card_min", _MIN_CARD)
        state = {
            "agent.id": "mode_roleplay/card_min",
            "execution_context": {"roleplay_persona": "不该出现的附身人设"},
        }

        text = material.build_injection(state, pkg)

        assert "# 对话示例" in text, "卡键路照常追加对话示例"
        assert "# 扮演接管" not in text
        assert "不该出现的附身人设" not in text

    def test_card_key_with_missing_card_returns_empty(self, pkg) -> None:
        """卡键成立但卡文件缺失 = 空串（不落 persona 分支）。"""
        state = {
            "agent.id": "mode_roleplay/card_ghost",
            "execution_context": {"roleplay_persona": "不该出现的附身人设"},
        }

        assert material.build_injection(state, pkg) == ""

    def test_corrupt_card_yaml_degrades_to_empty(self, pkg) -> None:
        """卡 yaml 损坏 = 降级空串不抛出（降级契约）。"""
        (pkg / "agents").mkdir(parents=True)
        (pkg / "agents" / "card_bad.yaml").write_text(
            "name: [截断\n", encoding="utf-8"
        )

        text = material.build_injection(
            {"agent.id": "mode_roleplay/card_bad"}, pkg
        )

        assert text == ""

    def test_corrupt_lorebook_degrades_to_empty(self, pkg) -> None:
        """世界书损坏 = 整体降级空串不抛出（降级契约，不半截注入）。"""
        _write_card(pkg / "agents", "card_min", _MIN_CARD)
        (pkg / "lorebooks").mkdir(parents=True)
        (pkg / "lorebooks" / "test_world.yaml").write_text(
            "entries: [截断\n", encoding="utf-8"
        )
        state = {"agent.id": "mode_roleplay/card_min",
                 "messages": [{"role": "user", "content": "空屋子"}]}

        assert material.build_injection(state, pkg) == ""


class TestUserLayerDualRoot:
    """用户层双根（用户物料 C1/C3 修复契约）：卡与世界书用户层优先 → 回落包内。

    调用方形态（mode_material_inject 注选传参）：user_agents_dir =
    user_config_dir()/agents（组装器内拼 mode_roleplay/）、books_dir =
    user_config_dir()/lorebooks；None = 仅包内（两参调用向后兼容，其余
    测试类已覆盖）。
    """

    @pytest.fixture
    def roots(self, tmp_path):
        """双根目录坐标（镜像真实布局，两侧不重叠）：

        - 包内（pkg_dir=tmp_path）：agents/ 与 lorebooks/；
        - 用户层（user_cfg 镜像 user_config_dir()）：agents/（组装器内拼
          mode_roleplay/）与 lorebooks/。
        """
        user_cfg = tmp_path / "user_cfg"
        return {
            "root": tmp_path,
            "agents_root": user_cfg / "agents",
            "user_cards": user_cfg / "agents" / "mode_roleplay",
            "books": user_cfg / "lorebooks",
        }

    def _state(self, card_id: str = "card_min", text: str = "空屋子") -> dict[str, Any]:
        return {"agent.id": f"mode_roleplay/{card_id}",
                "messages": [{"role": "user", "content": text}]}

    def test_user_card_overrides_package_card(self, roots) -> None:
        """同 id 双卡：用户层卡接管（对话示例以用户层内容为准，包内不出现）。"""
        _write_card(roots["user_cards"], "card_min", _MIN_CARD)
        _write_card(roots["root"] / "agents", "card_min",
                    dict(_MIN_CARD, mes_example="<START>\n{{user}}: 你好\n{{char}}: 包内版本不该出现"))

        text = material.build_injection(
            self._state(), roots["root"],
            user_agents_dir=str(roots["agents_root"]),
        )

        assert "测试角色: 你好啊。" in text, "用户层卡内容生效"
        assert "包内版本不该出现" not in text, "同 id 包内卡被接管"

    def test_user_only_card_is_assembled(self, roots) -> None:
        """包内无此卡、仅用户层有（导入/新建卡形态）：照常组出追加段。"""
        user_card = dict(_MIN_CARD, name="导入卡")
        _write_card(roots["user_cards"], "card_imported", user_card)

        text = material.build_injection(
            self._state("card_imported"), roots["root"],
            user_agents_dir=str(roots["agents_root"]),
        )

        assert "# 对话示例" in text
        assert "导入卡: 你好啊。" in text

    def test_user_card_missing_falls_back_to_package(self, roots) -> None:
        """用户层无此卡（出厂卡常态路径）：静默回落包内，包内内容生效。"""
        pkg_agents = roots["root"] / "agents"
        _write_card(pkg_agents, "card_min", _MIN_CARD)

        text = material.build_injection(
            self._state(), roots["root"],
            user_agents_dir=str(roots["agents_root"]),
        )

        assert "测试角色: 你好啊。" in text

    def test_corrupt_user_card_falls_back_to_package(self, roots, caplog) -> None:
        """用户层卡损坏 = warning 跳过回落包内（不整体降级、不静默换人）。"""
        _write_card(roots["user_cards"], "card_min", _MIN_CARD)
        (roots["user_cards"] / "card_min.yaml").write_text(
            "name: [截断\n", encoding="utf-8"
        )
        pkg_agents = roots["root"] / "agents"
        _write_card(pkg_agents, "card_min",
                    dict(_MIN_CARD, mes_example="<START>\n{{user}}: 你好\n{{char}}: 包内回落版"))

        with caplog.at_level("WARNING"):
            text = material.build_injection(
                self._state(), roots["root"],
                user_agents_dir=str(roots["agents_root"]),
            )

        assert "包内回落版" in text, "损坏用户卡跳过后回落包内"
        assert "用户层卡损坏" in caplog.text

    def test_corrupt_user_card_without_package_is_empty(self, roots) -> None:
        """用户层卡损坏且包内无同 id = 双根皆无 → 空串（键语义不静默换人设）。"""
        roots["user_cards"].mkdir(parents=True)
        (roots["user_cards"] / "card_ghost.yaml").write_text(
            "name: [截断\n", encoding="utf-8"
        )

        assert material.build_injection(
            self._state("card_ghost"), roots["root"],
            user_agents_dir=str(roots["agents_root"]),
        ) == ""

    def test_user_book_read_and_overrides_package(self, roots) -> None:
        """用户层书：命中注入用户条目；同 id 包内书不出现（接管）。"""
        _write_card(roots["root"] / "agents", "card_min", _MIN_CARD)
        _write_book(roots["books"], "test_world", _MIN_BOOK)
        _write_book(roots["root"] / "lorebooks", "test_world", {
            "entries": [{"keys": ["空屋子"], "content": "包内版本不该出现"}]
        })
        state = self._state()

        text = material.build_injection(state, roots["root"], books_dir=str(roots["books"]))

        assert "空屋子里有一把旧椅子。" in text
        assert "包内版本不该出现" not in text

    def test_user_book_missing_falls_back_to_package(self, roots) -> None:
        """用户层书缺失：回落包内同 id 书（包内出厂书不被用户目录屏蔽）。"""
        _write_card(roots["root"] / "agents", "card_min", _MIN_CARD)
        _write_book(roots["root"] / "lorebooks", "test_world", _MIN_BOOK)
        roots["books"].mkdir(parents=True)  # 用户层书目录存在但为空

        text = material.build_injection(
            self._state(), roots["root"], books_dir=str(roots["books"]))

        assert "空屋子里有一把旧椅子。" in text

    def test_corrupt_user_book_falls_back_to_package(self, roots, caplog) -> None:
        """用户层书损坏 = warning 跳过回落包内（不整体降级）。"""
        _write_card(roots["root"] / "agents", "card_min", _MIN_CARD)
        (roots["books"]).mkdir(parents=True)
        (roots["books"] / "test_world.yaml").write_text("entries: [截断\n", encoding="utf-8")
        _write_book(roots["root"] / "lorebooks", "test_world", _MIN_BOOK)

        with caplog.at_level("WARNING"):
            text = material.build_injection(
                self._state(), roots["root"], books_dir=str(roots["books"]))

        assert "空屋子里有一把旧椅子。" in text, "损坏用户书跳过后回落包内"
        assert "用户层世界书损坏" in caplog.text

    def test_corrupt_user_book_without_package_zero_lore_injection(self, roots) -> None:
        """用户层书损坏且包内无同 id = 该书零注入，但对话示例段不受牵连。"""
        _write_card(roots["root"] / "agents", "card_min", _MIN_CARD)
        (roots["books"]).mkdir(parents=True)
        (roots["books"] / "test_world.yaml").write_text("entries: [截断\n", encoding="utf-8")

        text = material.build_injection(
            self._state(), roots["root"], books_dir=str(roots["books"]))

        assert "## 世界设定" not in text
        assert "# 对话示例" in text

    @pytest.mark.parametrize("bad_id", ["../escape", "a/b", "中文名", ""])
    def test_illegal_book_id_zero_injection(self, roots, bad_id: str, caplog) -> None:
        """lorebook_ids 非白名单形态（穿越/分隔符/CJK/空）：按书缺失零注入
        （路径安全闸——卡 yaml 可携第三方导入内容）。"""
        card = dict(_MIN_CARD, lorebook_ids=[bad_id] if bad_id else [])
        _write_card(roots["root"] / "agents", "card_min", card)
        state = self._state()

        with caplog.at_level("WARNING"):
            text = material.build_injection(state, roots["root"], books_dir=str(roots["books"]))

        assert "## 世界设定" not in text
        if bad_id:
            assert "世界书 id 形态非法" in caplog.text


class TestSeedCardsConversationContract:
    """出厂三卡会话化契约（2026-09-25 扮演是对话不是任务）：卡 = 纯对话执行体。

    - 零工具面：tool_ids 显式空表（context_build 现有语义），卡不再携带
      task_evaluate；
    - system_prompt 无「收尾」段（评估收尾指令随评估义务一并移除）。
    """

    @pytest.mark.parametrize("card_id", ["card_luna", "card_kael", "card_mira"])
    def test_seed_cards_have_no_evaluation_surface(self, card_id: str) -> None:
        with open(os.path.join(ROLEPLAY_DIR, "agents", f"{card_id}.yaml"), encoding="utf-8") as fh:
            card = yaml.safe_load(fh)
        assert card["tool_ids"] == [], f"{card_id} 零工具面（显式空表）"
        assert "task_evaluate" not in (card.get("system_prompt") or "")
        assert "# 收尾" not in (card.get("system_prompt") or "")
        assert "# 扮演规则" in card["system_prompt"], "扮演规则段保持不动"


class TestOpeningSections:
    """会话化开演档注入段（execution_context.roleplay_greeting /
    roleplay_user_persona）：卡键路/旁路共用组段，无键零注入。"""

    _GREETING = "*提灯的光圈里，她正踮脚往书架塞书。*「呀——欢迎光临！」"
    _PERSONA = "北地来的佣兵，沉默寡言。"

    def test_card_key_path_injects_greeting_and_user_persona(self, tmp_path) -> None:
        """卡键路：开场白段 + 用户设定段按序追加（对齐旧开演派发段序）。"""
        _write_card(tmp_path / "agents", "card_min", _MIN_CARD)
        state = {
            "agent.id": "mode_roleplay/card_min",
            "execution_context": {
                "roleplay_greeting": self._GREETING,
                "roleplay_user_persona": self._PERSONA,
            },
        }

        text = material.build_injection(state, tmp_path)

        assert "# 本场开场白（用户已选定）" in text
        assert "以下面这段开场白开场，自然衔接后继续演出：" in text
        assert self._GREETING in text
        assert "# 用户设定（对话者扮演）" in text
        assert self._PERSONA in text
        # 段序：开场白先于用户设定
        assert text.index("# 本场开场白") < text.index("# 用户设定")
        # 段序：卡物料（对话示例）先于开演档
        assert text.index("# 对话示例") < text.index("# 本场开场白")

    def test_bypass_path_injects_greeting_alongside_takeover(self, tmp_path) -> None:
        """旁路（附身人设 + 开演档并存）：接管块与开场白段并存。"""
        state = {
            "agent.id": "agentos",
            "execution_context": {
                "roleplay_persona": "一位沉默寡言的老船长。",
                "roleplay_greeting": self._GREETING,
            },
        }

        text = material.build_injection(state, tmp_path)

        assert "# 扮演接管" in text
        assert "# 本场开场白（用户已选定）" in text
        assert self._GREETING in text

    @pytest.mark.parametrize(
        "context",
        [
            {},
            {"roleplay_greeting": "   ", "roleplay_user_persona": "  "},
            {"roleplay_greeting": 42, "roleplay_user_persona": None},
        ],
        ids=["absent", "blank", "non-string"],
    )
    def test_missing_or_invalid_keys_zero_injection(self, tmp_path, context: dict) -> None:
        """无键/纯空白/非字符串 = 零注入（不产出空段，不抛出）。"""
        state = {"agent.id": "mode_roleplay/card_min", "execution_context": context}
        _write_card(tmp_path / "agents", "card_min", _MIN_CARD)

        text = material.build_injection(state, tmp_path)

        assert "# 本场开场白" not in text
        assert "# 用户设定" not in text
        assert "# 对话示例" in text, "卡物料不受开演档缺席影响"

    def test_persona_only_without_greeting_keeps_user_persona(self, tmp_path) -> None:
        """只有用户设定（无开场白）也注入（两键独立，非捆绑）。"""
        state = {
            "agent.id": "mode_roleplay/card_min",
            "execution_context": {"roleplay_user_persona": self._PERSONA},
        }
        _write_card(tmp_path / "agents", "card_min", _MIN_CARD)

        text = material.build_injection(state, tmp_path)

        assert "# 本场开场白" not in text
        assert "# 用户设定（对话者扮演）" in text
        assert self._PERSONA in text
