"""角色扮演模式物料组装器（模式包 material.py 约定出口的本包实现）。

约定契约（通用模式物料步骤消费，见 plugins/shared/mode_material_inject/，
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
  卡人设、主 agent 身份不变）→ 人设替换不在本组装器：mode.yaml persona 声明
  （replace+from）由 mode_material_inject 通用机制写 context.persona_text，
  prompt_build 的 {{persona:}} 占位符换源（替换主 agent 人设段，骨架不动；
  追加式接管块已退役——双重人设且违背"直接替换"语义，2026-09-28）。本组装
  器旁路只剩开演档段。
- 世界书：卡 lorebook_ids 逐书扫描注入（scan_inject 按最近消息文本命中；
  书缺失/未命中 = 零注入）。书双根查找：用户层 books_dir/<book_id>.yaml 优先
  → 回落包内 lorebooks/<book_id>.yaml（同 id 用户接管；两目录参数皆可 None =
  仅包内，向后兼容两参调用）。
- 角色状态输出约定：卡键路追加包内 rules/state_conventions.md 整文段
  （<state> 标记输出契约——引擎解析步在 state_marker_parse，模型侧指令由
  本段承载，缺席即契约断链）；文件缺失/损坏 = 该段零注入（debug 记因，
  可选资产——tmp/简配包无 rules/ 属常态）。
- 用户输入 regex 脚本（RP 批 1a）：卡 extensions.regex_scripts 中
  affects=User Input 的脚本作用于最后一条用户消息（depth=0），改写以 set op
  暂存 STAGED_MESSAGE_OPS_KEY（通用步骤提升到 messages 合并面）；无脚本卡
  零暂存（兼容态零行为变化）；引擎 = 包内 regex_scripts.py。
- 开演档（会话化开演，卡键路/旁路共用）：execution_context.roleplay_greeting
  （所选开场白原文）+ roleplay_user_persona（用户设定）组段追加，无键零注入。
- 开演向导覆盖（RP 面板刀-Ⅰ，FR-A3/FR-G4；execution_context 键值经 mode.session
  extra_context 通道透传，桥 fail-closed 只收非空字符串——列表以逗号分隔 id 串
  承载）：wizard_lorebook_ids 覆盖卡默认 lorebook_ids（键缺席 = 现状零变化）；
  wizard_profile（能力/性格/难度会话级设定）居组装后缀段（键缺席零注入）。
  wizard_pack_ids 消费在装载侧 agent_config_load（增量并入卡 pack_ids），本组装
  器不涉；rp_axes（C2 整合刀四轴选择快照）注入消费=后续刀（键契约见下方
  常量区注释，本组装器暂未读该键属预期）。

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

# 卡 regex 脚本暂存键（本刀契约）：组装期对用户消息的脚本改写以 set op 列表
# 暂存本键，由通用步骤 mode_material_inject 提升到 state_updates["messages"]
# （{"_ops": [...]}，引擎 set 契约）——下划线键不参与引擎合并，暂存即过桥。
STAGED_MESSAGE_OPS_KEY = "_material_message_ops"

# 开演向导覆盖键（mode.session extra_context 通道契约；键缺席 = 现状零变化）：
# - wizard_lorebook_ids：本场所选世界书 id 串（逗号分隔），覆盖卡默认
#   lorebook_ids；
# - wizard_profile：会话级角色配置（能力/性格/难度自由文本），组装后缀段注入，
#   不回写卡资产、不碰提示词前缀；
# - wizard_pack_ids（增量并入卡 pack_ids）：消费在装载侧 agent_config_load；
# - rp_axes（C2 整合刀四轴选择快照，候选集=GET data/wizard/axes；k=v 对以逗号
#   串承载：scale=<explicit|implication|fade_to_black>,pace=<reasonable|
#   butterfly|normal>,agency=<you_control|director|input_improver>[,style=
#   <styles/ 文风档 id>]，文风不选=无 style 对，三缺省档恒在）。注入消费=
#   后续刀：按档拼文风纪律段（styles/ 档 traits）/尺度段/节奏段/代理权段——
#   本刀只落候选+选择+快照，此处未读该键属预期。
WIZARD_LOREBOOK_IDS_KEY = "wizard_lorebook_ids"
WIZARD_PROFILE_KEY = "wizard_profile"


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
        _apply_user_input_scripts(state, card)
        example = _render_card_examples(card)
        if example:
            sections.append(example)
        lore = _render_lorebooks(state, _effective_lorebook_ids(state, card), pkg_dir, books_dir)
        if lore:
            sections.append(lore)
        conventions = _render_state_conventions(pkg_dir)
        if conventions:
            sections.append(conventions)
    opening = _render_opening_sections(state)
    if opening:
        sections.append(opening)
    return "\n\n".join(sections)


def _effective_lorebook_ids(state: dict[str, Any], card: dict[str, Any]) -> list[Any]:
    """本场生效世界书 id 列表：向导覆盖（wizard_lorebook_ids）优先，回落卡默认。"""
    override = _wizard_id_list(state, WIZARD_LOREBOOK_IDS_KEY)
    if override is not None:
        return override
    ids = card.get("lorebook_ids")
    return ids if isinstance(ids, list) else []


def _wizard_id_list(state: dict[str, Any], key: str) -> list[str] | None:
    """向导 id 串读出（逗号分隔）：键缺席/非字符串/纯空白 = None（无覆盖）；
    有值 = 去空段 id 列表（空段容忍，如 "a,,b"）。"""
    raw = _execution_context_text(state, key)
    if not raw:
        return None
    return [segment.strip() for segment in raw.split(",") if segment.strip()]


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
    - roleplay_user_persona（用户设定，对话者扮演）：对话者身份告知；
    - wizard_profile（向导会话级角色配置，能力/性格/难度）：居末位后缀段。
    段序对齐旧开演派发 goal_description（开场白 → 用户设定 → 向导配置）。
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
    profile = _execution_context_text(state, WIZARD_PROFILE_KEY)
    if profile:
        sections.append(f"# 会话角色配置（开演向导）\n{profile}")
    return "\n\n".join(sections)


def _render_lorebooks(state: dict[str, Any], ids: list[Any], pkg_dir, books_dir) -> str:
    """本场世界书逐书双根查找（用户层 books_dir 优先 → 包内 lorebooks/）+
    扫描注入；命中为空 / 无绑定书 = 空串（零注入）。id 列表由调用方给
    （向导覆盖优先、回落卡默认，见 _effective_lorebook_ids）。"""
    if not ids:
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


def _render_state_conventions(pkg_dir) -> str:
    """卡键路状态标记约定段：包内 rules/state_conventions.md 直读整文注入。

    <state> 标记输出契约（引擎解析步在 state_marker_parse 插件）的模型侧
    指令由本段承载；文件缺失/损坏 = 空串零注入（debug 记因，可选资产）。
    """
    path = Path(pkg_dir) / "rules" / "state_conventions.md"
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        logger.debug(
            "[mode_roleplay.material] 状态约定文件不可读，该段零注入 | path=%s | err=%s",
            path,
            exc,
        )
        return ""
    return text


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


def _apply_user_input_scripts(state: dict[str, Any], card: dict[str, Any]) -> None:
    """卡 regex 脚本（affects=User Input）作用于最后一条用户消息（depth=0）。

    改写以 set op 暂存 state[STAGED_MESSAGE_OPS_KEY]（STAGED_MESSAGE_OPS_KEY
    契约见常量处注释），不就地改 messages——state 是本步快照，就地改不落账。
    引擎 = 包内 regex_scripts.py（真实导入名 mode_roleplay.regex_scripts，
    播种自包含）；变量变更 warning 记录不落账（批 2 Validator 收编前临时桥）。
    无脚本/消息缺失/内容非字符串/无 seq/零改写 = 零暂存；任何异常 warning
    降级（不拖垮物料组装的降级契约）。
    """
    try:
        extensions = card.get("extensions")
        scripts = extensions.get("regex_scripts") if isinstance(extensions, dict) else None
        if not isinstance(scripts, list) or not scripts:
            return
        msg = _last_user_message(state)
        if msg is None or not isinstance(msg.get("content"), str) or msg.get("seq") is None:
            return
        regex = _load_pkg_module("regex_scripts", "regex_scripts.py")
        new_text, var_changes = regex.apply_scripts(
            str(msg["content"]), scripts, "User Input", 0, {}
        )
        if var_changes:
            logger.warning(
                "[mode_roleplay.material] regex 变量变更（批 2 前仅记录不落账）| vars=%s",
                var_changes,
            )
        if new_text == msg["content"]:
            return
        staged = state.get(STAGED_MESSAGE_OPS_KEY)
        staged = staged if isinstance(staged, list) else []
        new_msg = dict(msg)
        new_msg["content"] = new_text
        staged.append({"op": "set", "seq": msg["seq"], "msg": new_msg})
        state[STAGED_MESSAGE_OPS_KEY] = staged
    except Exception as exc:  # noqa: BLE001 — 降级契约：脚本面失败不阻断物料组装
        logger.warning("[mode_roleplay.material] 用户输入脚本应用降级 | err=%s", exc)


def _last_user_message(state: dict[str, Any]) -> dict[str, Any] | None:
    """最后一条 role=user 消息（用户输入脚本的作用对象；无 = None）。"""
    messages = state.get("messages")
    if not isinstance(messages, list):
        return None
    for msg in reversed(messages):
        if isinstance(msg, dict) and msg.get("role") == "user":
            return msg
    return None


def _load_pkg_module(rel_name: str, file_name: str):
    """按真实导入名（mode_roleplay.<rel_name>）装载包内模块（防双实例）。"""
    name = f"mode_roleplay.{rel_name}"
    mod = sys.modules.get(name)
    if mod is not None:
        return mod
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), file_name)
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"{file_name} spec 构造失败: {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _load_lorebook():
    """包内 lorebook.py 装载（真实导入名 mode_roleplay.lorebook）。"""
    return _load_pkg_module("lorebook", "lorebook.py")
