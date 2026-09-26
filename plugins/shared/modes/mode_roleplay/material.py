"""角色扮演模式物料组装器（模式包 material.py 约定出口的本包实现）。

约定契约（通用模式物料步骤消费，见 plugins/shared/pipeline/input/
mode_material_inject/plugin.py）：暴露 ``build_injection(state, pkg_dir)``
返回本模式追加注入文本（无则空串）。种子自包含：不 import 共享根模块，
依赖仅标准库 + PyYAML + 包内 lorebook.py（按真实导入名 mode_roleplay.lorebook）。

职责（基础模式段之外的 roleplay 专属追加物料）：
- 卡键路（state agent.id 为本包卡键 mode_roleplay/<card_id>）→ 双根读卡
  （用户层 user_agents_dir/mode_roleplay/<card_id>.yaml 优先 → 回落包内
  agents/<card_id>.yaml；用户导入/新建卡由此进扮演组装），只追加 mes_example
  对话示例解析（占位符替换：{{char}}→卡名、{{user}}→中性称谓「用户」）+
  世界书扫描——卡接管声明与设定/性格/场景由 context_build 按卡键加载的卡
  yaml system_prompt 承载，此处重复注入即双份；
- 旁路（非卡键但 execution_context.roleplay_persona 非空，附身场景前端透传
  卡人设、主 agent 身份不变）→ 用该文本组接管块（此场景 context_build 无卡
  数据，接管声明+人设全量必要）；卡键成立则 persona 分支不参与（卡优先）。
- 世界书：卡 lorebook_ids 逐书扫描注入（scan_inject 按最近消息文本命中；
  书缺失/未命中 = 零注入）。书双根查找：用户层 books_dir/<book_id>.yaml 优先
  → 回落包内 lorebooks/<book_id>.yaml（同 id 用户接管；两目录参数皆可 None =
  仅包内，向后兼容两参调用）。
- 开演档（会话化开演，卡键路/旁路共用）：execution_context.roleplay_greeting
  （所选开场白原文）+ roleplay_user_persona（用户设定）组段追加，无键零注入。

降级契约：任何失败（卡缺失/解析失败/书损坏）→ 返回空串（debug 记因），
绝不阻断管道；warning 由通用步骤统一打。用户层文件缺失/损坏 → warning 跳过
回落包内（用户层是可变面，坏件不当缺席也不当致命）；包内文件损坏保持既有
语义（上抛 → 整体降级空串，出厂种子损坏属包事实）。
"""
from __future__ import annotations

import importlib.util
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# 卡键形态：mode_roleplay/<stem>（stem 限字母数字下划线，杜绝路径分隔符与
# `..` 穿越；口径与共享根 mode_keys 一致但不 import 之——种子自包含约束）。
_CARD_KEY_RE = re.compile(r"^mode_roleplay/([A-Za-z0-9_]+)$")

# 世界书 id 白名单（路径安全闸）：lorebook_ids 出自卡 yaml（用户层卡可携第三方
# 导入内容），非白名单形态按书缺失处理（零注入），杜绝分隔符/`..` 穿越。
_BOOK_ID_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")

# mes_example 占位符替换：{{char}} → 卡名；{{user}} 无名单可取，用中性称谓。
USER_PLACEHOLDER_NAME = "用户"


def build_injection(state: dict[str, Any], pkg_dir, books_dir=None, user_agents_dir=None) -> str:
    """模式包组装器约定出口：返回本模式追加注入文本（无则空串）。

    Args:
        state: 管道 state（agent.id / execution_context / messages 等自行取用）。
        pkg_dir: 本模式包目录（通用步骤经 find_package_dir 解析后传入）。
        books_dir: 用户层世界书目录（None = 仅包内 lorebooks/；双根查找用户层
            优先，同 id 接管，缺失回落包内）。
        user_agents_dir: 用户层 agents 目录（None = 仅包内 agents/；卡查找：
            <user_agents_dir>/mode_roleplay/<card_id>.yaml 优先 → 回落包内）。

    两目录参数均为预留扩展点（通用约定出口 build_injection(state, pkg_dir)
    两参形态向后兼容，调用方按组装器签名注选传参）。
    """
    try:
        return _build(state, pkg_dir, books_dir, user_agents_dir)
    except Exception as exc:  # noqa: BLE001 — 降级契约：任何失败返回空串不阻断
        logger.debug("[mode_roleplay.material] 组装降级返回空串 | err=%s", exc)
        return ""


def _build(state: dict[str, Any], pkg_dir, books_dir, user_agents_dir) -> str:
    agent_id = str(state.get("agent.id", "") or "")
    matched = _CARD_KEY_RE.match(agent_id)
    sections = []
    if matched is not None:
        card = _read_card(pkg_dir, matched.group(1), user_agents_dir)
        if card is None:
            # 卡键成立但双根皆无卡 = 组不出追加段（不落 persona 分支——
            # 键语义已声明用卡，静默换人设会演错角色）。
            return ""
        example = _render_card_examples(card)
        if example:
            sections.append(example)
        lore = _render_lorebooks(state, card, pkg_dir, books_dir)
        if lore:
            sections.append(lore)
    else:
        persona = _roleplay_persona(state)
        if persona:
            sections.append(_render_persona_takeover(persona))
    opening = _render_opening_sections(state)
    if opening:
        sections.append(opening)
    return "\n\n".join(sections)


def _read_card(pkg_dir, card_id: str, user_agents_dir=None) -> dict[str, Any] | None:
    """读卡（双根）：用户层 <user_agents_dir>/mode_roleplay/<card_id>.yaml 优先
    → 回落包内 agents/<card_id>.yaml。

    用户层缺失 = 静默回落（出厂卡常态路径）；用户层损坏（解析失败/顶层非
    映射）= warning 跳过回落包内；两侧皆缺 = None；包内损坏上抛（外层降级
    空串——出厂种子损坏属包事实，不静默当缺席）。
    """
    if user_agents_dir:
        path = Path(user_agents_dir) / "mode_roleplay" / f"{card_id}.yaml"
        if path.is_file():
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8"))
            except (OSError, yaml.YAMLError) as exc:
                logger.warning(
                    "[mode_roleplay.material] 用户层卡损坏，回落包内 | card=%s | err=%s",
                    card_id,
                    exc,
                )
            else:
                if isinstance(data, dict):
                    return data
                logger.warning(
                    "[mode_roleplay.material] 用户层卡顶层非映射，回落包内 | card=%s",
                    card_id,
                )
    path = Path(pkg_dir) / "agents" / f"{card_id}.yaml"
    if not path.is_file():
        return None
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else None


def _render_card_examples(card: dict[str, Any]) -> str:
    """卡键路追加段：mes_example 对话示例（占位符替换；示例缺省 = 空串零注入）。

    设定/性格/场景不在此重复——卡键场景 context_build 已加载卡 yaml 的
    system_prompt（自带同内容），追加即双份注入。
    """
    raw = str(card.get("mes_example") or "").strip()
    if not raw:
        return ""
    name = str(card.get("name") or card.get("display_name") or "").strip() or "角色"
    example = raw.replace("{{char}}", name).replace("{{user}}", USER_PLACEHOLDER_NAME)
    return "\n".join(["# 对话示例", example])


def _render_persona_takeover(persona: str) -> str:
    """附身场景接管块：接管声明 + 前端透传的卡人设文本。"""
    return "\n".join([
        "# 扮演接管",
        "你将接管以下人设进行沉浸式角色扮演，永远保持角色身份。",
        "",
        "# 人设",
        persona.strip(),
    ])


def _roleplay_persona(state: dict[str, Any]) -> str:
    """读 execution_context.roleplay_persona（可选字符串）；无/空返回空串。"""
    return _execution_context_text(state, "roleplay_persona")


def _execution_context_text(state: dict[str, Any], key: str) -> str:
    """execution_context 可选字符串键读出（无/非字符串/纯空白 = 空串）。"""
    ec = state.get("execution_context")
    value = ec.get(key) if isinstance(ec, dict) else None
    if isinstance(value, str) and value.strip():
        return value.strip()
    return ""


def _render_opening_sections(state: dict[str, Any]) -> str:
    """会话化开演档注入段（卡键路/旁路共用；无键 = 空串零注入）：

    - roleplay_greeting（所选开场白原文）：约束 AI 以该开场白开场自然衔接；
    - roleplay_user_persona（用户设定，对话者扮演）：对话者身份告知。
    段序对齐旧开演派发 goal_description（开场白 → 用户设定）。
    """
    sections = []
    greeting = _execution_context_text(state, "roleplay_greeting")
    if greeting:
        sections.append(
            "# 本场开场白（用户已选定）\n以下面这段开场白开场，自然衔接后继续演出：\n"
            f"{greeting}"
        )
    user_persona = _execution_context_text(state, "roleplay_user_persona")
    if user_persona:
        sections.append(f"# 用户设定（对话者扮演）\n{user_persona}")
    return "\n\n".join(sections)


def _render_lorebooks(state: dict[str, Any], card: dict[str, Any], pkg_dir, books_dir) -> str:
    """卡绑定世界书逐书双根查找（用户层 books_dir 优先 → 包内 lorebooks/）+
    扫描注入；命中为空 / 无绑定书 = 空串（零注入）。"""
    ids = card.get("lorebook_ids")
    if not isinstance(ids, list) or not ids:
        return ""
    texts = _recent_texts(state)
    lorebook = _load_lorebook()
    blocks: list[str] = []
    for book_id in ids:
        book = _load_book(pkg_dir, books_dir, lorebook, str(book_id))
        if book is None:
            continue  # 双根皆缺 / id 形态非法 = 零注入（契约：lorebook 缺失=零注入）
        blocks.append(lorebook.scan_inject(book, texts))
    return "\n\n".join(block for block in blocks if block)


def _load_book(pkg_dir, books_dir, lorebook, book_id: str) -> dict[str, Any] | None:
    """世界书双根查找：用户层 <books_dir>/<book_id>.yaml 优先 → 包内
    lorebooks/<book_id>.yaml。

    用户层缺失 = 静默回落；用户层损坏 = warning 跳过回落包内；包内缺失 =
    None；包内损坏上抛（外层降级空串）；id 非白名单形态 = None（路径安全闸）。
    """
    if not _BOOK_ID_RE.fullmatch(book_id):
        logger.warning(
            "[mode_roleplay.material] 世界书 id 形态非法，按缺失跳过 | book=%r", book_id
        )
        return None
    if books_dir:
        path = Path(books_dir) / f"{book_id}.yaml"
        if path.is_file():
            try:
                return lorebook.load_book(str(path))
            except ValueError as exc:  # load_book 契约：损坏抛 ValueError
                logger.warning(
                    "[mode_roleplay.material] 用户层世界书损坏，回落包内 | book=%s | err=%s",
                    book_id,
                    exc,
                )
    path = Path(pkg_dir) / "lorebooks" / f"{book_id}.yaml"
    if not path.is_file():
        return None
    return lorebook.load_book(str(path))


def _recent_texts(state: dict[str, Any]) -> list[str]:
    """最近消息文本（世界书扫描窗口）。

    0.2 用户输入落点 = state["messages"]（route_user_input 只写 messages 键，
    user_input 键 0.2 无生产者）；input/user_input 字符串键仅作旁路兜底。
    多模态 content（块列表）取其中的 text 块。
    """
    texts: list[str] = []
    messages = state.get("messages")
    if isinstance(messages, list):
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            content = msg.get("content")
            if isinstance(content, str):
                if content.strip():
                    texts.append(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        text = str(part.get("text") or "")
                        if text.strip():
                            texts.append(text)
    for key in ("input", "user_input"):
        value = state.get(key)
        if isinstance(value, str) and value.strip():
            texts.append(value)
    return texts


def _load_lorebook():
    """按真实导入名（mode_roleplay.lorebook）装载包内 lorebook.py（防双实例）。"""
    name = "mode_roleplay.lorebook"
    mod = sys.modules.get(name)
    if mod is not None:
        return mod
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lorebook.py")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"lorebook spec 构造失败: {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod
