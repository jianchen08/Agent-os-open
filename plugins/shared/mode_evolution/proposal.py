"""提案八条静态校验（纯函数，机械执行）。

提案语义（MVP）：整文件替换——content 为提案后的完整文件内容，target 为主仓
相对路径。八条全部通过才允许落 staging；任一失败返回违规清单。
技能类提案（target 在 skills/ 下）第 8 项条件触发：过 skill_audit 安检
（提示注入/命令面/附件声明面，fail-closed）——出厂 skills/ 运行期唯一写路径
= 本提案管道（原则 7，ADR 2026-10-09-mode-system-consolidation 决策 7），
管道即技能进入系统的唯一闸门。

内容提案（扩展波 B-Ⅱ，FR-F6）：target 落 RP 出厂内容三目录（mode_roleplay
agents/lorebooks/packs 下 .yaml）走同管道进出厂——这是 skills/ 独占写先例的
内容物料版（出厂内容运行期唯一写路径 = 本提案管道，人审批准环节在 apply 面）。
安检第 8 项条件触发换成 content_audit（结构三面 + suspicious 注入形状
fail-closed 阻断；HTML 高危只标注随提案落账，渲染面消毒兜底——与创作 lint/
import 面同政策，禁第四套安检；判定单源在 mode_roleplay card_lint，本面经
card 质检服务消费，服务不可用 fail-closed 拒绝）。其余 plugins/shared/modes/ 面
（插件代码/声明面）保持冻结语义不变。
"""
from __future__ import annotations

import hashlib
import json
import os
import posixpath
from collections.abc import Awaitable, Callable
from typing import Any

import skill_audit

# card 质检服务调用通道（mode_roleplay provides namespace card，T6c；server.py
# on_load 注入 / on_unload 摘除）：content_audit 的结构 lint + 内容安检消费缝。
_card_service_caller: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]] | None = None


def set_card_service_caller(
    caller: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]] | None,
) -> None:
    """注入/清除 card 质检服务调用通道（server.py on_load/on_unload 调用）。"""
    global _card_service_caller
    _card_service_caller = caller

# ② 冻结面（路径前缀一票拒绝）——B 部类 + 内核 + 受控区 + 安全红线文件
# + 裁决权载体（ADR 2026-09-16：评估/复盘面整体冻结——阈值参数直接改变
# 判定松紧，被裁决者不可经提案改裁判，P3 一票否决的结构性防线）。
# 例外：mode_roleplay 三内容目录下的 .yaml 走内容提案面（content_kind 判定
# 前置豁免），其余 mode_* 面照冻结（拍平布局 ADR 2026-10-13：模式包平铺
# 共享根，mode_ 前缀识别）。
FROZEN_PREFIXES = (
    "config/self_evolve/",
    "plugins/shared/mode_",
    "plugins/shared/evaluation/",
    "plugins/shared/review/",
    "config/plugins/evaluation/",
    "config/plugins/review/",
    "kernel/",
    ".env",
    "config/kernel/",
    "config/plugins/isolation/",
    "config/rules/information_integrity_rules.md",
    "config/rules/document_context_rules.md",
    "tests/eval_bench/",
)

# ③ 路径白名单（A 部类可改面，MVP 用前缀表近似登记表）
# skills/ = 全局技能物料（原则 7 进化独占写：运行期唯一写路径 = 本管道，
# 提案必过技能安检，登记表 material_registry.md 同步登记）
# mode_roleplay 三内容目录 = RP 出厂内容面（B-Ⅱ，提案必过内容安检）。
EVOLVABLE_PREFIXES = (
    "config/agents/",
    "config/pipelines/",
    "config/rules/",
    "skills/",
    "plugins/shared/auto_gen_",
    "plugins/shared/mode_roleplay/agents/",
    "plugins/shared/mode_roleplay/lorebooks/",
    "plugins/shared/mode_roleplay/packs/",
)

REQUIRED_FIELDS = ("layer", "target", "change_type", "content")
CHANGE_TYPES = {"param_edit", "content_edit", "list_add", "list_remove", "new_plugin"}

# 内容提案面：出厂目录 → 内容型（卡/世界书/玩法包 yaml；仅 .yaml 可提案，
# 目录内其余文件不属内容面、冻结语义照挡）。卡 = prompt 载体主战场。
_CONTENT_PREFIX_KINDS: tuple[tuple[str, str], ...] = (
    ("plugins/shared/mode_roleplay/agents/", "card"),
    ("plugins/shared/mode_roleplay/lorebooks/", "lorebook"),
    ("plugins/shared/mode_roleplay/packs/", "pack"),
)

# 出厂内容所在种子插件 manifest（内容落地后装机生效前提：版本 bump——
# BuiltinFirst 同 id 双源裁决要求提案副本严格大于内置副本；实际 bump 由
# 人审批准环节执行，提案面只登记提示）。
_CONTENT_SEED_MANIFEST = "plugins/shared/mode_roleplay/plugin.json"


def file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _norm(path: str) -> str:
    p = str(path).replace("\\", "/")
    # normpath 收敛冗余段：中段 "./" 与 ".."（"config/agents/../kernel"→"kernel"、
    # "./x"→"x"；前导 ".." 保留由逃逸检查兜底）——前缀表（冻结/白名单/内容面）
    # 因此吃归一后路径，借道白名单前缀触碰冻结面进不来。".env" 等相对锚文件名
    # 不受 normpath 影响（无分隔符原样返回）。
    return posixpath.normpath(p)


def content_kind(target: str) -> str | None:
    """target → 内容提案型（card/lorebook/pack）；非内容面返回 None。

    仅 .yaml 后缀 + 三内容目录前缀命中才算内容提案——目录内 README/脚本等
    非内容文件不豁免冻结面。
    """
    norm = _norm(target)
    if not norm.endswith(".yaml"):
        return None
    for prefix, kind in _CONTENT_PREFIX_KINDS:
        if norm.startswith(prefix):
            return kind
    return None


def content_version_note(project_root: str) -> str:
    """出厂内容落地注记：种子插件当前版本 + 装机生效 bump 提示（人审环节执行）。

    manifest 不可读时如实降级提示（不虚构版本号）。
    """
    version = ""
    manifest = os.path.join(project_root, _CONTENT_SEED_MANIFEST)
    try:
        with open(manifest, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            version = str(data.get("version") or "")
    except (OSError, ValueError):
        version = ""
    tail = f"（现 {version}）" if version else "（版本号不可读，请人工核对）"
    return (
        f"出厂内容提案：{_CONTENT_SEED_MANIFEST} 版本须 bump{tail}并 sync 装机"
        "用户空间后方生效（BuiltinFirst：版本须严格大于内置副本；实际 bump 由"
        "人审批准环节执行）"
    )


# card 质检服务不可用时的 fail-closed 违规文案（安检面缺席 = 无法验证 = 拒绝）。
_CARD_SERVICE_UNAVAILABLE = "content: 卡质检服务不可用（fail-closed 拒绝）"


async def _card_call(method: str, params: dict[str, Any]) -> dict[str, Any] | None:
    """card 质检服务调用；未注入/抛错/信封无效 = None（调用方 fail-closed 处置）。"""
    if _card_service_caller is None:
        return None
    try:
        result = await _card_service_caller(method, params)
    except Exception:  # noqa: BLE001 —— 服务失败统一按不可用处置（安检 fail-closed）
        return None
    if not isinstance(result, dict) or "error" in result:
        return None
    return result


async def content_audit(target: str, content: str, project_root: str) -> tuple[list[str], list[str]]:
    """内容提案安检（判定单源 mode_roleplay card_lint，经 card 质检服务消费）：
    返回（违规, 标注）。

    阻断面（fail-closed，任一命中即拒）：yaml 可解析且为映射；卡 name 非空；
    结构三面（卡 extensions / 玩法包顶层 variables+behaviors——同一判定函数，
    card.lint_extensions）；世界书 entries 形态；suspicious 注入形状（卡=
    description+system_prompt 拼文，世界书=条目 content 拼文，玩法包=全字符串
    叶子拼文，card.scan_suspicious）。服务不可用 = 无法验证 = fail-closed 拒绝。
    标注面（不拒绝随提案落账）：高危 HTML 形态（card.scan_html_*，渲染面消毒
    兜底，与创作 lint/import 面同政策）；服务不可用时标注缺席（阻断面已拒绝）。
    """
    kind = content_kind(target)
    if kind is None or not str(content).strip():
        return [], []
    try:
        import yaml  # noqa: PLC0415

        data = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        return [f"content: yaml 解析失败: {exc}"], []
    if not isinstance(data, dict):
        return ["content: 内容须为 yaml 映射"], []

    violations: list[str] = []
    annotations: list[str] = []
    # card_lint 产出的警告统一加 content: 前缀（skill_audit: 同例——拒绝原因
    # 指明安检面）；标注面不前缀（随提案落账的注记，非拒绝原因）。
    def _flagged(warnings: list[str]) -> list[str]:
        return [f"content: {warning}" for warning in warnings]

    async def _blocking(method: str, params: dict[str, Any]) -> list[str]:
        """阻断面服务调用：不可用 = fail-closed 违规。"""
        result = await _card_call(method, params)
        return _flagged(result.get("warnings") or []) if result is not None else [_CARD_SERVICE_UNAVAILABLE]

    async def _annotating(method: str, params: dict[str, Any]) -> list[str]:
        """标注面服务调用：不可用 = 标注缺席（不拒绝）。"""
        result = await _card_call(method, params)
        return (result.get("warnings") or []) if result is not None else []

    if kind == "card":
        if not str(data.get("name") or "").strip():
            violations.append("content: 卡缺非空 name（id 由名派生的前提）")
        violations.extend(await _blocking("lint_extensions", {"extensions": data.get("extensions")}))
        haystack = str(data.get("description") or "") + str(data.get("system_prompt") or "")
        violations.extend(await _blocking("scan_suspicious", {"text": haystack, "subject": "角色设定"}))
        annotations.extend(await _annotating("scan_html_card", {"card": data}))
    elif kind == "lorebook":
        entries = data.get("entries")
        if entries is not None and not isinstance(entries, list):
            violations.append("content: 世界书 entries 须为列表")
        texts = [
            str(entry.get("content") or "")
            for entry in (entries or [])
            if isinstance(entry, dict)
        ]
        violations.extend(await _blocking("scan_suspicious", {"text": "\n".join(texts), "subject": "世界书内容"}))
        annotations.extend(await _annotating("scan_html_texts", {"texts": texts, "subject": "世界书内容"}))
    else:  # pack：顶层 {variables, behaviors} 过 extensions 同一结构判定
        violations.extend(await _blocking("lint_extensions", {"extensions": data}))
        texts_result = await _card_call("texts_of_tree", {"data": data})
        texts = (texts_result.get("texts") or []) if texts_result is not None else []
        if texts_result is None:
            violations.append(_CARD_SERVICE_UNAVAILABLE)
        violations.extend(await _blocking("scan_suspicious", {"text": "\n".join(texts), "subject": "玩法包内容"}))
        annotations.extend(await _annotating("scan_html_texts", {"texts": texts, "subject": "玩法包内容"}))
    return violations, annotations


async def validate(proposal: dict[str, Any], project_root: str) -> list[str]:
    """返回违规清单；空列表 = 通过。"""
    violations: list[str] = []

    # ① schema 完整性
    for field in REQUIRED_FIELDS:
        if not str(proposal.get(field) or "").strip():
            violations.append(f"schema: 缺必填字段 {field}")
    if proposal.get("change_type") not in CHANGE_TYPES:
        violations.append(f"schema: change_type 非法 {proposal.get('change_type')!r}")
    if violations:
        return violations

    target = _norm(str(proposal["target"]))
    root_abs = os.path.abspath(project_root)
    abs_target = os.path.abspath(os.path.join(root_abs, target))
    kind = content_kind(target)

    # 逃逸检查：target 解析后必须仍在项目根内
    if os.path.commonpath([root_abs, abs_target]) != root_abs:
        violations.append(f"whitelist: 目标路径逃逸项目根 {target}")
        return violations

    # ② 冻结面（前缀一票拒绝；内容提案面 = 三内容目录 yaml，豁免仅限
    # content_kind 命中的目标，其余 plugins/shared/modes/ 面照冻结）
    if kind is None:
        for prefix in FROZEN_PREFIXES:
            if target.startswith(prefix):
                violations.append(f"frozen: 触碰冻结面 {prefix}*")
                return violations

    # ③ 路径白名单
    if not any(target.startswith(p) for p in EVOLVABLE_PREFIXES):
        violations.append(f"whitelist: 目标不在可改面内 {target}")

    # ④ 原子性（MVP：单文件提案）
    if "\n---PROPOSAL-FILE---\n" in str(proposal.get("content", "")):
        violations.append("atomicity: 多文件提案（MVP 仅支持单文件）")

    # ⑤ base 一致性（目标文件当前内容哈希与提案声明一致；新文件豁免——
    # 内容提案面新文件 = 出厂内容首提案常态，同样豁免）
    base_hash = str(proposal.get("base_hash") or "").strip()
    if os.path.isfile(abs_target):
        current = file_sha256(abs_target)
        if base_hash and base_hash != current:
            violations.append("base: 目标文件已前移（base_hash 不符），请基于最新内容重新生成")
    elif kind is None and proposal["change_type"] != "new_plugin" and proposal.get("layer") != "L3":
        violations.append("base: 目标文件不存在且提案非新增类")

    # ⑥ 写面唯一：由 server 层落盘保证（staging 目录），此处校验提案未内嵌绝对路径
    if re_search_abs_path(str(proposal.get("content", ""))):
        violations.append("write_scope: content 内嵌绝对路径（应使用相对路径与占位符）")

    # ⑦ 题面相似度：MVP 跳过（嵌入检索二期），记录 skipped 不计违规

    # ⑧ 安检（条件触发，任一命中即拒 fail-closed）：技能提案过 skill_audit；
    # 内容提案过 content_audit（判定单源 mode_roleplay card_lint，服务消费，
    # 违规信息指明命中项）——两条出厂写路径各自的管道闸门
    if kind is not None:
        audit_violations, _annotations = await content_audit(
            target, str(proposal.get("content", "")), project_root)
        violations.extend(audit_violations)
    elif target.startswith("skills/"):
        violations.extend(
            skill_audit.audit(target, str(proposal.get("content", "")), project_root)
        )
    return violations


def re_search_abs_path(content: str) -> bool:
    import re
    return bool(re.search(r"[A-Za-z]:\\\\|(?<![\"'\w])/(?:Users|home)/", content))
