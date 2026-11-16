"""技能落地器 + 来源登记（外部技能进用户根的唯一写路径）。

落点 = ``<user_root>/skills/<skill_id>/``（用户根解析在工具边界经
``agentos_plugin_sdk.diagnostics_settings.user_root`` 完成，与内核 ``user_space``
同解析）。仓根 ``skills/`` 是出厂件，其运行期唯一写路径是自进化提案管道
（``mode_evolution.skill_audit`` 即该管道闸门）——本模块不写仓根。

闸门顺序（全部前置；任一失败整批拒且零写入）：许可证 → 技能 id 安全校验 →
批内重名 → 目标同名存在（``force=false``）→ 内容审查（复用
``mode_evolution.skill_audit.audit``，fail-closed）。许可证「缺席」可经显式
开关 ``allow_undeclared_license`` 放行并在登记册留痕（``license_override``）；
清单内已知且被排除的许可证（如 ``Proprietary``）不受该开关影响。

MCP 写面（``apply_mcp=true`` 才动，见 :mod:`mcp_config`）在所有闸门之后、技能
提交之前执行：配置面拒写/写失败时零技能写入；技能提交失败时配置已写入的事实
由错误信息如实带出。溯源登记册按写面结果记 ``mcp_written``。

提交语义：整批技能先复制进 ``<user_root>/skills`` 下的临时暂存目录，全部复制
成功后才逐一 rename 到目标位（同卷换入）——复制阶段失败零目标写入，不留半成品；
暂存目录由 ``tempfile.TemporaryDirectory`` 退出时回收。

落地为**真值同步**：``force=true`` 先整删目标目录再换入本次快照，残留文件不复活。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import mcp_config
from _root_api import atomic_write_text as _atomic_write_text
from import_ir import PluginIR, SkillAsset
from license_gate import check_license
from mode_evolution import skill_audit

__all__ = [
    "ApplyReport",
    "LandError",
    "LandedSkill",
    "ON_SKILL_REJECT_ABORT",
    "ON_SKILL_REJECT_SKIP",
    "RejectedSkill",
    "apply_plugin",
    "audit_skill",
    "git_head_commit",
    "validate_skill_id",
]

# 批内技能闸门失败策略（apply_plugin 的 on_skill_reject 取值）：
#   abort — 任一技能失败即整批拒、零写入（默认，原子裁决）
#   skip  — 跳过该技能并如实上报，其余照常落地（显式选择；真机实测 56 包中
#           7 包因单技能被封死，此开关解封而闸门判据不放松）
ON_SKILL_REJECT_ABORT = "abort"
ON_SKILL_REJECT_SKIP = "skip"
_ON_SKILL_REJECT = frozenset({ON_SKILL_REJECT_ABORT, ON_SKILL_REJECT_SKIP})

PROVENANCE_FILENAME = "ecosystem_imports.json"
PROVENANCE_SCHEMA_VERSION = 1
SKILLS_DIRNAME = "skills"
STAGING_PREFIX = ".ecosystem-import-staging-"
SKILL_FILENAME = "SKILL.md"
SKILL_ID_RE = re.compile(r"\A[A-Za-z0-9._-]+\Z")
_RESERVED_SKILL_IDS = frozenset({".", ".."})
_GIT_TIMEOUT_SECONDS = 10
_ANCHOR_SEARCH_DEPTH = 4


class LandError(Exception):
    """落地/治理闸门拒收（错误是值：工具边界翻译成失败信封）。"""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class LandedSkill:
    """一个已落地技能（skill_id → 用户根目标目录）。"""

    skill_id: str
    path: Path


@dataclass(frozen=True)
class RejectedSkill:
    """一个被批内闸门拒收的技能（``skip`` 模式下如实上报，不静默丢弃）。"""

    skill_id: str
    code: str
    reason: str


@dataclass(frozen=True)
class ApplyReport:
    """落地结果：已提交技能 + 被拒技能 + 溯源登记册路径 + 许可证豁免留痕 + MCP 写面结果。"""

    skills: tuple[LandedSkill, ...]
    rejected: tuple[RejectedSkill, ...]
    provenance_path: Path
    license_override: bool
    mcp: mcp_config.McpWriteReport | None = None


def validate_skill_id(skill_id: str) -> str | None:
    """技能 id 安全校验（路径穿越 / 非法字符）；返回问题说明，None = 合法。

    空串由字符白名单 ``+`` 量词直接拒（无独立空值分支）。
    """
    if skill_id in _RESERVED_SKILL_IDS:
        return f"技能 id 是路径穿越形态：{skill_id!r}"
    if not SKILL_ID_RE.match(skill_id):
        return f"技能 id 含非法字符（只允许 A-Za-z0-9._-）：{skill_id!r}"
    return None


def _audit_target(skill_dir: Path) -> tuple[str, str]:
    """闸门 target 与 project_root：以**真实树**最近的 ``skills/`` 祖先为解析根。

    闸门的 target 形态约束是 ``skills/<…>/SKILL.md``——它按 ``project_root + 该相对
    路径`` 定位技能目录（声明面两向核对），并按 ``project_root`` 解析跨技能引用。
    外部布局里技能目录不一定落在导入插件根的 ``skills/`` 下（裸技能根形态即
    ``<skills 根>/<名>``，市场清单形态可指向任意相对路径），故锚不能取插件根，
    必须按技能目录自身的真实相对位置表达。

    Raises:
        LandError: 上溯不到 ``skills/`` 祖先（闸门对该形态整体 no-op，静默放过即
            声明面失守，故拒落而非放行）。
    """
    for ancestor in list(skill_dir.parents)[:_ANCHOR_SEARCH_DEPTH]:
        if ancestor.name == SKILLS_DIRNAME:
            rel = skill_dir.relative_to(ancestor).as_posix()
            return f"{SKILLS_DIRNAME}/{rel}/{SKILL_FILENAME}", str(ancestor.parent)
    raise LandError(
        "技能目录不在任何 skills/ 下（闸门锚要求路径含 skills/ 段，拒落）："
        f"{skill_dir}——请将技能置于名为 skills 的目录下再导入",
        "AUDIT_ANCHOR_UNSUPPORTED",
    )


def audit_skill(skill: SkillAsset) -> list[str]:
    """单技能内容审查（空列表 = 通过）。

    范围取 ``doc_scope="package"``：外部技能包按目录约定分工（脚本说明可写在
    同目录兄弟 .md，如 superpowers 的 ``brainstorming`` 经 ``visual-companion.md``
    说明脚本）——声明面 = 技能目录内全部 .md，脚本仍须被本技能自己的文档点名
    （ADR 2026-10-17-skill-audit-package-scope）。闸门锚按真实树表达（见
    ``_audit_target``），技能 id 一律取磁盘目录名（frontmatter name 可与之不同）。

    Raises:
        LandError: 技能正文不可读 / 锚形态不支持（均 fail-closed，不跳过该技能）。
    """
    skill_md = skill.source_dir / SKILL_FILENAME
    try:
        content = skill_md.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise LandError(f"技能正文不可读（拒落）：{skill_md}（{exc}）", "AUDIT_UNREADABLE") from exc
    target, anchor = _audit_target(skill.source_dir)
    return skill_audit.audit(target, content, anchor, doc_scope=skill_audit.DOC_SCOPE_PACKAGE)


def git_head_commit(source_dir: Path) -> str | None:
    """源目录所属 git 仓库的 HEAD commit；取不到（非 git 仓 / git 不可用）→ None。

    commit 是「时间点快照」的身份位：外部包按快照导入，无自动跟随机制。
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(source_dir), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    sha = proc.stdout.strip()
    return sha if proc.returncode == 0 and sha else None


def apply_plugin(
    ir: PluginIR,
    *,
    user_root: Path,
    force: bool,
    on_skill_reject: str = ON_SKILL_REJECT_ABORT,
    allow_undeclared_license: bool = False,
    apply_mcp: bool = False,
) -> ApplyReport:
    """把 IR 的技能落地到 ``<user_root>/skills`` 并登记溯源（``apply_mcp`` 时同刀写 MCP 配置）。

    Args:
        ir: 翻译器产出的插件 IR。
        user_root: 用户空间根（调用方解析，本层不读环境）。
        force: ``True`` 覆盖同名已落地技能（真值同步）；``False`` 时目标已存在即拒。
        on_skill_reject: 批内技能闸门（id 安全/批内重名/目标同名/内容审查）失败时的策略。
            ``abort``（默认）= 任一技能失败即整批拒、零写入（原子裁决）；
            ``skip``（显式选择）= 跳过该技能并如实上报，其余照常落地——
            整包被单个技能封死的场景（真机实测 56 包中 7 包）由此可部分导入。
            两种模式下许可证闸门与登记册真值均为**批级**失败面，不受该选项影响。
        allow_undeclared_license: 显式开关：True 时放行**未声明**许可证
            （``ir.license`` 为 None/空串）；清单内已知且被排除的许可证
            （如 ``Proprietary``）不受该开关影响，照旧拒收。放行时溯源登记册
            记 ``license: null`` + ``license_override: true``（用了开关才记 true）。
        apply_mcp: 显式开关：True 时把源 ``.mcp.json`` 的非 OAuth 条目合并进当前
            配置根（:func:`mcp_config.write_mcp_servers`），并在登记册记
            ``mcp_written``；False（默认）不触碰配置面。

    Raises:
        LandError: 选项非法（非布尔开关/未知策略）/ 许可证拒收 / 全部技能被拒 /
            登记册不可解析 / 写入失败；MCP 写面的配置面错误同族翻译
            （``resolve_config_root``/``write_mcp_servers`` 的 :class:`mcp_config.McpConfigError`
            在此转 ``LandError``，错误码沿用）。
    """
    if on_skill_reject not in _ON_SKILL_REJECT:
        raise LandError(
            f"未知 on_skill_reject：{on_skill_reject!r}（合法值：{sorted(_ON_SKILL_REJECT)}）",
            "OPTION_INVALID",
        )
    if not isinstance(allow_undeclared_license, bool):
        raise LandError(
            f"allow_undeclared_license 需布尔值（不静默做真值转换）：{allow_undeclared_license!r}",
            "OPTION_INVALID",
        )
    if not isinstance(apply_mcp, bool):
        raise LandError(
            f"apply_mcp 需布尔值（不静默做真值转换）：{apply_mcp!r}",
            "OPTION_INVALID",
        )
    gate = check_license(ir.license, license_file=ir.license_file, allow_undeclared=allow_undeclared_license)
    if not gate.allowed:
        raise LandError(f"许可证闸门 {gate.reason}", "LICENSE_REJECTED")
    # 豁免留痕：仅「缺席 + 开关放行」记 True；已声明许可证不算豁免
    license_override = gate.license is None

    skills_root = user_root / SKILLS_DIRNAME
    accepted, rejected = _partition_by_batch_gates(ir, skills_root, force, on_skill_reject)
    accepted = _partition_by_audit(accepted, ir, on_skill_reject, rejected)
    if ir.skills and not accepted:
        detail = "；".join(f"{item.skill_id}[{item.code}]" for item in rejected)
        raise LandError(f"全部技能未通过（无落地项）：{detail}", "ALL_SKILLS_REJECTED")

    mcp_report = _write_mcp(ir) if apply_mcp else None
    provenance_path = user_root / PROVENANCE_FILENAME
    doc = _load_provenance(provenance_path)
    try:
        committed = _stage_and_commit(accepted, skills_root)
    except LandError as exc:
        if mcp_report is None or not mcp_report.written:
            raise
        raise LandError(f"{exc}；MCP 配置已写入（{len(mcp_report.written)} 条）", exc.code) from exc
    _write_provenance(provenance_path, _merge_provenance(doc, ir, committed, license_override, mcp_report))
    return ApplyReport(
        skills=committed,
        rejected=tuple(rejected),
        provenance_path=provenance_path,
        license_override=license_override,
        mcp=mcp_report,
    )


def _write_mcp(ir: PluginIR) -> mcp_config.McpWriteReport:
    """MCP 写面（配置根解析 + 合并）；配置面错误翻译为同族 ``LandError``。

    配置根解析失败属环境面，与配置拒写同码面（``CONFIG_ROOT_UNRESOLVED``）。
    """
    try:
        return mcp_config.write_mcp_servers(ir, config_root=mcp_config.resolve_config_root())
    except mcp_config.McpConfigError as exc:
        raise LandError(str(exc), exc.code) from exc


def _partition_by_batch_gates(
    ir: PluginIR, skills_root: Path, force: bool, on_skill_reject: str
) -> tuple[list[SkillAsset], list[RejectedSkill]]:
    """批内技能闸门：id 安全 → 批内重名 → 目标同名存在（``abort`` 即抛，``skip`` 记拒）。"""
    accepted: list[SkillAsset] = []
    rejected: list[RejectedSkill] = []
    seen: set[str] = set()
    for skill in ir.skills:
        problem = validate_skill_id(skill.skill_id)
        code = "SKILL_ID_INVALID"
        if problem is not None:
            problem = f"技能 id 非法（拒落）：{problem}"
        elif skill.skill_id in seen:
            code, problem = "SKILL_ID_DUPLICATE", f"批内技能 id 重复（拒落）：{skill.skill_id}"
        elif not force and (skills_root / skill.skill_id).exists():
            code = "SKILL_EXISTS"
            problem = f"目标技能目录已存在（拒落）：{skills_root / skill.skill_id}——同源重导请显式 force=true 覆盖"
        if problem is None:
            seen.add(skill.skill_id)
            accepted.append(skill)
        elif on_skill_reject == ON_SKILL_REJECT_ABORT:
            raise LandError(problem, code)
        else:
            rejected.append(RejectedSkill(skill_id=skill.skill_id, code=code, reason=problem))
    return accepted, rejected


def _partition_by_audit(
    accepted: list[SkillAsset], ir: PluginIR, on_skill_reject: str, rejected: list[RejectedSkill]
) -> list[SkillAsset]:
    """内容审查面：过闸的留，未过的按策略抛或记拒。"""
    passed: list[SkillAsset] = []
    for skill in accepted:
        try:
            violations = audit_skill(skill)
        except LandError as exc:
            if on_skill_reject == ON_SKILL_REJECT_ABORT:
                raise
            rejected.append(RejectedSkill(skill_id=skill.skill_id, code=exc.code, reason=str(exc)))
            continue
        if not violations:
            passed.append(skill)
            continue
        reason = "内容审查未通过（拒落）：" + "；".join(violations)
        if on_skill_reject == ON_SKILL_REJECT_ABORT:
            raise LandError(reason, "AUDIT_REJECTED")
        rejected.append(RejectedSkill(skill_id=skill.skill_id, code="AUDIT_REJECTED", reason=reason))
    return passed


def _stage_and_commit(skills: list[SkillAsset], skills_root: Path) -> tuple[LandedSkill, ...]:
    """整批复制进暂存目录后逐一换入目标位；复制/换入失败 → LAND_FAILED（已提交项数如实报告）。"""
    committed: list[LandedSkill] = []
    try:
        skills_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=STAGING_PREFIX, dir=skills_root) as staging:
            staged: list[tuple[SkillAsset, Path]] = []
            for skill in skills:
                copy = Path(shutil.copytree(skill.source_dir, Path(staging) / skill.skill_id))
                staged.append((skill, copy))
            for skill, staged_dir in staged:
                target = skills_root / skill.skill_id
                if target.exists():
                    shutil.rmtree(target)
                staged_dir.replace(target)
                committed.append(LandedSkill(skill_id=skill.skill_id, path=target))
    except OSError as exc:
        raise LandError(f"技能落地失败（已提交 {len(committed)}/{len(skills)} 项）：{exc}", "LAND_FAILED") from exc
    return tuple(committed)


def _import_key(record: dict[str, Any]) -> tuple[str, str]:
    """溯源幂等键：源目录 + 插件名（同源重导原地刷新）。"""
    return (str(record.get("source_dir") or ""), str(record.get("plugin") or ""))


def _provenance_record(
    ir: PluginIR,
    committed: tuple[LandedSkill, ...],
    license_override: bool,
    mcp_report: mcp_config.McpWriteReport | None,
) -> dict[str, Any]:
    """本次导入的溯源记录（版本/许可证/来源/commit/时间/技能清单/翻译器版本/豁免留痕）。

    ``mcp_written`` 只在 MCP 写面被请求时出现（值 = 实际写入的服务器名，空列表即
    已请求但无条目可写）；未请求该写面则无此键（与历史记录同为「无该写面」语义）。
    """
    record: dict[str, Any] = {
        "plugin": ir.name,
        "version": ir.version,
        "license": ir.license,
        "license_override": license_override,
        "source_dir": str(ir.source_dir),
        "source_commit": git_head_commit(ir.source_dir),
        "imported_at": datetime.now(UTC).isoformat(),
        "skills": [item.skill_id for item in committed],
        "translator": {"kind": ir.kind, "version": ir.translator_version},
    }
    if mcp_report is not None:
        record["mcp_written"] = [item.name for item in mcp_report.written]
    return record


def _load_provenance(path: Path) -> dict[str, Any]:
    """读溯源登记册；不存在 = 空册。

    Raises:
        LandError: 登记册存在但不可解析或结构非法（治理真值不容覆盖，fail-closed）。
    """
    if not path.is_file():
        return {"version": PROVENANCE_SCHEMA_VERSION, "imports": []}
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LandError(
            f"溯源登记册不可解析（拒覆盖）：{path}（{type(exc).__name__}: {exc}）",
            "PROVENANCE_INVALID",
        ) from exc
    if not isinstance(doc, dict) or not isinstance(doc.get("imports"), list):
        raise LandError(f"溯源登记册结构非法（拒覆盖）：{path}", "PROVENANCE_INVALID")
    return doc


def _merge_provenance(
    doc: dict[str, Any],
    ir: PluginIR,
    committed: tuple[LandedSkill, ...],
    license_override: bool,
    mcp_report: mcp_config.McpWriteReport | None,
) -> dict[str, Any]:
    """按幂等键更新登记册（同源重导替换原记录，不重复登记；其余记录原样保留）。"""
    record = _provenance_record(ir, committed, license_override, mcp_report)
    key = _import_key(record)
    imports = [item for item in doc["imports"] if not (isinstance(item, dict) and _import_key(item) == key)]
    imports.append(record)
    merged = dict(doc)
    merged["version"] = PROVENANCE_SCHEMA_VERSION
    merged["imports"] = imports
    return merged


def _write_provenance(path: Path, doc: dict[str, Any]) -> None:
    """原子写溯源登记册（同目录 tmp + os.replace；父目录按需建）。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(path, json.dumps(doc, ensure_ascii=False, indent=2) + "\n")
    except OSError as exc:
        raise LandError(f"技能已落地但溯源登记写入失败：{path}（{exc}）", "PROVENANCE_FAILED") from exc
