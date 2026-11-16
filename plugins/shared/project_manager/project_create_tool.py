"""project_create 工具——创建项目（= 真实文件夹 + 登记行 + 类型初始化）。

项目与任务解耦（2026-08-30 用户裁定）：项目是任务树分组锚点，不执行、
无执行者；创建走独立入口（本工具 / 会话目录登记 / 前端表单），任务经
task_submit 的 project_id 挂靠后在其文件夹下执行（默认 worktree 分叉）。
同目录已登记时幂等复用既有项目（不重复建文件夹/登记行）。

项目类型出生初始化（2026-09-03 用户裁定）：类型 → 初始化配方映射在
config/tools/project_create.yaml（通用配置，新增类型 = 加条目不改代码）。
显式传 project_type 路由对应初始化；缺省 auto 按 detect_file 识别已有项目。
初始化失败整体报错，同路径重跑幂等补装。
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

from agentos_plugin_sdk.builtin_tool import BuiltinTool  # noqa: E402
from agentos_plugin_sdk.results import ToolExecutionResult  # noqa: E402
from agentos_plugin_sdk.tool_types import (  # noqa: E402
    Tool,
    ToolCategory,
    ToolLevel,
    ToolSource,
    create_failure_result,
    create_success_result,
)

# 仓库根（project_create_tool.py → project_manager/ → shared/ → plugins/ → 根）
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_TYPE_CONFIG_PATH = _PROJECT_ROOT / "config" / "tools" / "project_create.yaml"

_PROJECT_INPUT_SCHEMA_STATIC: dict[str, Any] = {
    "type": "object",
    "properties": {
        "goal": {
            "type": "string",
            "minLength": 1,
            "description": (
                "项目标题（必填）。项目 = 真实文件夹 + 登记行（任务树分组锚点），"
                "不执行、无执行者；任务经 task_submit 的 project_id 挂靠后"
                "在项目文件夹下执行（默认 worktree 从项目仓库分叉）。"
            ),
        },
        "path": {
            "type": "string",
            "description": (
                "项目文件夹（可选）。显式指定目录；缺省自动生成"
                "{工作空间根}/projects/<标题>。已存在目录非 git 仓库会自动"
                "git init（不删改现有文件）；同目录已登记时复用既有项目。"
            ),
        },
        "mode_tags": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "项目登记模式标签集合（可选，批 B8）。开放标签（非枚举），"
                "每个标签须为已注册模式包（出厂/用户双根可解析，如 coding/"
                "writing/godot/research），登记后 task_submit 显式带 mode 派发"
                "时校验 mode ∈ 本集合；同路径复用既有项目时显式传入即补挂/"
                "调整登记（不传则不动既有登记）。写入时各标签包的 skills/ 随之"
                "播种到项目根 skills/（增量补缺不覆盖用户改动）。"
            ),
        },
        "default_mode": {
            "type": "string",
            "description": (
                "主域标注（可选，批 B8）。必须是 mode_tags 之一；仅作说明面"
                "消费（方案段第零步主域确定、AGENTS.md 主域标注），不做派发"
                "回退。"
            ),
        },
    },
    "required": ["goal"],
}

# project_type 的不变行为契约（跨类型恒定，不属于配置映射面）
_PROJECT_TYPE_CONTRACT = (
    "auto（缺省）：目录已有该类型的清单文件时自动按该类型初始化。"
    "未声明类型报错；初始化失败整体报错，同路径重跑幂等补装。"
)

# 映射唯一真值 = config/tools/project_create.yaml：schema_summary（每类型说明）
# 决定 description、类型清单决定 enum。plugin.json 的声明是本生成结果的同步
# 投影，由 TestManifestSchemaLockstep 锁步校验（配置改动不同步 manifest = 红）。


def _build_project_type_property(types: dict[str, Any]) -> dict[str, Any]:
    """由配方表生成 project_type 的 schema（枚举 + 映射说明）。"""
    mapping_text = "；".join(
        f"{name}：{str(recipe.get('schema_summary') or '').strip() or '（配方未写 schema_summary）'}"
        for name, recipe in types.items()
    )
    description = (
        "项目类型 → 出生初始化映射（唯一真值 config/tools/project_create.yaml，"
        "本说明与枚举由其生成）。"
        + (mapping_text + "。" if mapping_text else "当前未声明任何类型，显式指定将报错。")
        + _PROJECT_TYPE_CONTRACT
    )
    return {
        "type": "string",
        "enum": ["auto", *types.keys()],
        "default": "auto",
        "description": description,
    }


def _build_input_schema(types: dict[str, Any]) -> dict[str, Any]:
    """完整 input_schema = 静态骨架 + 配置生成的 project_type。"""
    properties = dict(_PROJECT_INPUT_SCHEMA_STATIC["properties"])
    properties["project_type"] = _build_project_type_property(types)
    return {
        "type": "object",
        "properties": properties,
        "required": list(_PROJECT_INPUT_SCHEMA_STATIC["required"]),
    }

_PROJECT_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["project_id", "title", "path", "status", "created"],
    "properties": {
        "project_id": {"type": "string", "description": "项目 id（12hex；复用既有时同值）"},
        "title": {"type": "string", "description": "项目标题"},
        "path": {"type": "string", "description": "项目文件夹宿主绝对路径"},
        "status": {"type": "string", "description": "active | paused"},
        "workflow_state": {
            "type": "string",
            "description": "方案工作流状态 plan | running | done（ADR 2026-09-17）",
        },
        "created": {"type": "boolean", "description": "true=新建登记；false=复用既有"},
        "session_id": {"type": "string", "description": "创建时关联的会话（可选）"},
        "mode_tags": {
            "type": "array",
            "items": {"type": "string"},
            "description": "项目登记模式标签集合（未登记为空数组）",
        },
        "default_mode": {
            "type": "string",
            "description": "主域标注（mode_tags 子集，未标注为空串）",
        },
        "warnings": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "技能播种告警（登记成功照常返回；包不可解析/单技能复制失败"
                "在此可见，不静默）"
            ),
        },
        "project_type": {"type": "string", "description": "路由到的项目类型（未路由为空串）"},
        "init": {
            "type": ["object", "null"],
            "description": "出生初始化执行明细（未路由为 null）",
            "properties": {
                "addons_installed": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "本次新装的宿主插件名",
                },
                "addons_present": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "已就位未重复安装的插件名",
                },
                "project_file_created": {
                    "type": "boolean",
                    "description": "类型清单文件是否本次脚手架创建",
                },
                "enabled_added": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "本次新增的启用条目",
                },
                "ci": {
                    "type": "object",
                    "description": (
                        "CI 包出生组装报告（设计 docs/working/项目类型CI包设计_20261030.md"
                        " §6.2：启用/跳过的面 + 判定原因）"
                    ),
                    "properties": {
                        "installed": {
                            "type": "boolean",
                            "description": "是否执行了 CI 包装配",
                        },
                        "packs": {
                            "type": "object",
                            "description": "装配的 pack → 出厂版本",
                        },
                        "packs_installed": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "本次实际拷写工厂物料的 pack",
                        },
                        "packs_skipped": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "跳过的 pack 与原因（未出厂/版本闸）",
                        },
                        "gates_total": {
                            "type": "integer",
                            "description": "组装后门禁总数",
                        },
                        "hook_installed": {
                            "type": "boolean",
                            "description": "pre-commit 钩子是否本次写入",
                        },
                        "hook_skipped_reason": {
                            "type": "string",
                            "description": "钩子未写入的原因（已有同名不覆盖等，空串=已写入）",
                        },
                        "probes": {
                            "type": "object",
                            "description": "出生工具探测结果（godot_bin 路径或空串）",
                        },
                        "warnings": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "装配告警（钩子冲突/local 未触碰等，不静默）",
                        },
                    },
                },
                "committed": {
                    "type": "boolean",
                    "description": "初始化后是否执行了 git 提交（无变更则跳过为 false）",
                },
            },
        },
    },
}


def _load_type_config() -> dict[str, Any]:
    """读项目类型配方表（config/tools/project_create.yaml）；文件缺失 = 未配置任何类型。"""
    if not _TYPE_CONFIG_PATH.is_file():
        return {}
    data = yaml.safe_load(_TYPE_CONFIG_PATH.read_text(encoding="utf-8")) or {}
    return data.get("project_types") or {}


def _match_recipe(
    project_path: str, requested: str, config: dict[str, Any]
) -> tuple[str, dict[str, Any]] | None:
    """解析类型路由：显式 project_type 优先，否则按 detect_file 自动识别。

    显式指定未声明类型抛 ValueError（fail-closed，禁止静默忽略调用方意图）；
    auto 未命中返回 None（普通项目，无初始化）。
    """
    if requested and requested != "auto":
        recipe = config.get(requested)
        if recipe is None:
            available = ", ".join(sorted(config)) or "配置缺失"
            raise ValueError(f"未知项目类型: {requested}（已声明: {available}）")
        return requested, recipe
    for ptype, recipe in config.items():
        detect = str(recipe.get("detect_file") or "")
        if detect and (Path(project_path) / detect).is_file():
            return ptype, recipe
    return None


def _merge_enabled_plugins(project_file: Path, section: str, entries: list[str]) -> list[str]:
    """把启用条目并入 project.godot 的 [editor_plugins] enabled（并集保序、幂等）。

    返回本次新增条目。enabled 行统一重写为单行 PackedStringArray（Godot 官方写法）；
    section/键缺失则在文件尾补全。失败即抛，禁止静默半写。
    """
    rendered = ", ".join(f'"{e}"' for e in entries)
    text = project_file.read_text(encoding="utf-8")
    sec_re = re.compile(rf"^\[{re.escape(section)}\]\s*$", re.MULTILINE)

    m = sec_re.search(text)
    if m is None:
        block = f"[{section}]\n\nenabled=PackedStringArray({rendered})\n"
        text = text.rstrip("\n") + "\n\n" + block if text.strip() else block
        project_file.write_text(text, encoding="utf-8")
        return entries

    nxt = sec_re.search(text, m.end())
    body = text[m.end() : nxt.start() if nxt else len(text)]
    head_re = re.compile(
        r"^enabled\s*=\s*(.*?)(?=^[A-Za-z_][A-Za-z0-9_]*\s*=|^\[|\Z)", re.MULTILINE | re.DOTALL
    )
    km = head_re.search(body)
    if km is None:
        line = f"\nenabled=PackedStringArray({rendered})\n"
        project_file.write_text(text[: m.end()] + line + text[m.end() :], encoding="utf-8")
        return entries
    existing = re.findall(r'"([^"]+)"', km.group(1))
    added = [e for e in entries if e not in existing]
    if added:
        merged_rendered = ", ".join(f'"{e}"' for e in existing + added)
        start, end = m.end() + km.start(1), m.end() + km.end(1)
        replacement = f"PackedStringArray({merged_rendered})"
        if km.group(1).endswith("\n"):
            replacement += "\n"
        project_file.write_text(text[:start] + replacement + text[end:], encoding="utf-8")
    return added


def _ensure_git_repo(project_path: str) -> None:
    """确保项目目录是 git 仓（CI 钩子装载与出生提交的前置；幂等）。"""
    root = Path(project_path)
    if (root / ".git").exists():
        return
    completed = subprocess.run(
        ["git", "init"], cwd=str(root), capture_output=True, text=True, timeout=60
    )
    if completed.returncode != 0:
        raise RuntimeError(f"git init 失败: {completed.stderr.strip()}")


def _git_commit_all(project_path: str, message: str) -> bool:
    """把项目文件夹当前状态提交入库（出生初始化产物 + 未跟踪工作区）。

    用户裁定（2026-09-03）：初始化 → 提交 → 再执行——worktree 只物化已提交文件，
    出生不提交则子任务 worktree 缺项目文件，godot 等全挂。仓库无提交身份时以
    -c 内联（不改全局配置）；无变更时跳过（幂等）。失败即抛（fail-closed）。
    本仓出生提交带 --no-verify：CI 钩子与物料同批落盘，执法面自检不阻塞项目
    创建；首次执法发生在下一次常规提交。
    """
    identity = ["-c", "user.name=agentos", "-c", "user.email=agentos@local"]
    root = Path(project_path)

    def _git(*args: str, check: bool = True) -> tuple[int, str]:
        completed = subprocess.run(
            ["git", *identity, *args], cwd=str(root), capture_output=True, text=True, timeout=60
        )
        if check and completed.returncode != 0:
            raise RuntimeError(f"git {args[0]} 失败: {completed.stderr.strip()}")
        return completed.returncode, completed.stdout

    _ensure_git_repo(project_path)
    _git("add", "-A")
    rc, status = _git("status", "--porcelain", check=False)
    if rc != 0 or not status.strip():
        return False
    _git("commit", "--no-verify", "-m", message)
    return True


def _apply_init_recipe(project_path: str, title: str, recipe: dict[str, Any]) -> dict[str, Any]:
    """执行出生初始化配方：装 addon → 补类型清单文件 → 合并启用条目 → 提交。

    幂等：已存在的 addon 不覆盖、enabled 并集；返回各步增量供输出断言。
    任何一步失败即抛（调用方整体报错），禁止静默半装。
    """
    result: dict[str, Any] = {
        "addons_installed": [],
        "addons_present": [],
        "project_file_created": False,
        "enabled_added": [],
        "committed": False,
    }
    src_root = Path(str(recipe.get("addons_source") or ""))
    if not src_root.is_absolute():
        src_root = _PROJECT_ROOT / src_root
    addons_root = Path(project_path) / "addons"
    for name in recipe.get("addons") or []:
        src, dst = src_root / str(name), addons_root / str(name)
        if not src.is_dir():
            raise FileNotFoundError(f"addon 安装源缺失: {src}")
        if dst.is_dir():
            result["addons_present"].append(str(name))
        else:
            shutil.copytree(src, dst)
            result["addons_installed"].append(str(name))

    pfile = Path(project_path) / str(recipe.get("project_file") or "")
    if not pfile.is_file():
        scaffold = str(recipe.get("scaffold") or "").replace("{title}", title)
        pfile.parent.mkdir(parents=True, exist_ok=True)
        pfile.write_text(scaffold, encoding="utf-8")
        result["project_file_created"] = True

    entries = [str(e) for e in recipe.get("enable_entries") or []]
    if entries:
        result["enabled_added"] = _merge_enabled_plugins(
            pfile, str(recipe.get("enable_section") or "editor_plugins"), entries
        )

    # CI 包出生组装（设计 docs/working/项目类型CI包设计_20261030.md §4.1/§5）：
    # 钩子要写 .git/hooks，先确保仓库在位；组装失败整体上抛（fail-closed）。
    _ensure_git_repo(project_path)
    from project_manager import project_ci_assembly  # noqa: PLC0415 —— 共享根延迟导入

    result["ci"] = project_ci_assembly.apply_ci_packs(
        project_path, recipe, _load_type_config()
    )

    result["committed"] = _git_commit_all(project_path, "chore: agentos 项目初始化（配方产物 + 工作区快照）")
    return result


class ProjectCreateTool(BuiltinTool):
    """项目创建工具。"""

    @staticmethod
    def get_tool_definition() -> Tool:
        return Tool(
            name="project_create",
            description=(
                "创建项目（= 真实文件夹 + 登记行 + 类型出生初始化）。项目是任务树分组锚点，"
                "不执行、无执行者；创建后把 project_id 传给 task_submit 挂靠，"
                "任务即在项目文件夹下执行（默认 worktree 分叉）。"
                "同目录已登记时复用既有项目（created=false）并幂等补装类型初始化。"
                "project_type 参数见映射说明。"
            ),
            input_schema=_build_input_schema(_load_type_config()),
            output_schema=_PROJECT_OUTPUT_SCHEMA,
            source=ToolSource.CODE,
            category=ToolCategory.TASK,
            level=ToolLevel.L1_L2_ONLY,
            tags=["project", "create"],
            injected_params=["user_id", "session_id"],
        )

    async def execute(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        """创建项目：建文件夹（显式路径优先，缺省 {ws_base}/projects/<slug>）+
        非 git 自动 git init + 登记；同路径已登记时幂等复用。登记成功后按
        类型配方执行出生初始化（失败整体报错，重跑幂等补装）。"""
        goal = str(inputs.get("goal") or "").strip()
        if not goal:
            return create_failure_result(
                error="必须指定项目标题（goal）——项目 = 文件夹 + 登记，标题是文件夹命名与登记标题的来源",
                error_code="MISSING_GOAL",
            )
        explicit_path = str(inputs.get("path") or "").strip()
        requested_type = str(inputs.get("project_type") or "auto").strip() or "auto"
        # mode 维度登记（批 B8）：未传 = None（复用既有项目不动其登记）；
        # 显式传入（含空数组）= 建时登记 / 复用补挂，校验在登记写面执法。
        mode_tags = inputs.get("mode_tags")
        mode_tags_arg: list[str] | None = (
            [str(t) for t in mode_tags] if isinstance(mode_tags, list) else None
        )
        default_mode_arg: str | None = (
            str(inputs.get("default_mode") or "").strip()
            if "default_mode" in inputs
            else None
        )

        from project_manager import project_registry  # noqa: PLC0415

        try:
            project, created = project_registry.ensure_project_registered(
                title=goal,
                explicit_path=explicit_path,
                session_id=str(inputs.get("session_id") or ""),
                submitted_by=str(inputs.get("user_id") or ""),
                mode_tags=mode_tags_arg,
                default_mode=default_mode_arg,
            )
        except (ValueError, RuntimeError) as exc:
            logger.error("[ProjectCreate] 项目创建失败 | goal=%s | err=%s", goal, exc)
            return create_failure_result(
                error=f"项目创建失败: {exc}",
                error_code="PROJECT_CREATE_FAILED",
            )
        # 技能播种（批 B8）：写入 mode_tags 的登记把各标签包 skills/ 增量补缺
        # 到项目根（幂等重跑即对账补缺）；失败不阻断登记，告警回传不静默。
        seed_warnings = project_registry.seed_mode_skills(project)

        routed: tuple[str, dict[str, Any]] | None
        init_info: dict[str, Any] | None = None
        try:
            routed = _match_recipe(project.path, requested_type, _load_type_config())
            if routed is not None:
                init_info = _apply_init_recipe(project.path, project.title, routed[1])
        except (ValueError, OSError) as exc:
            logger.error(
                "[ProjectCreate] 类型初始化失败 | project_id=%s | type=%s | path=%s | err=%s",
                project.id,
                requested_type,
                project.path,
                exc,
            )
            return create_failure_result(
                error=(
                    f"项目已登记（project_id={project.id}，重跑同路径幂等补装），"
                    f"但类型初始化失败: {exc}"
                ),
                error_code="PROJECT_INIT_FAILED",
            )

        logger.info(
            "[ProjectCreate] %s项目 | project_id=%s | title=%s | path=%s | user=%s | type=%s",
            "复用已登记" if not created else "创建",
            project.id,
            project.title,
            project.path,
            project.submitted_by or "-",
            routed[0] if routed else "-",
        )
        return create_success_result(
            data={
                "project_id": project.id,
                "title": project.title,
                "path": project.path,
                "status": project.status,
                "workflow_state": project.workflow_state,
                "created": created,
                "session_id": project.session_id or "",
                "mode_tags": list(project.mode_tags),
                "default_mode": project.default_mode,
                "warnings": seed_warnings,
                "project_type": routed[0] if routed else "",
                "init": init_info,
            }
        )
