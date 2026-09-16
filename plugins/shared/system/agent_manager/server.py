#!/usr/bin/env python3
"""Agent Manager 服务端——agent 配置聚合注册表（原内核 /api/v1/agents* 4 路由语义承接）。

端点（http.handle 按 path 分发，协议与 task_form/monitoring 同款）：
- GET  /ext/agent_manager/agents                    列表（?agent_type= 过滤，{items,total,
                                                    mode_registry}；系统注册表 + 模式包注册表
                                                    聚合，条目带 source: system|mode:<plugin_id>；
                                                    schema 取不到时降级仅系统表并注明）
- GET  /ext/agent_manager/agents/schema             字段声明（硬编码 12 字段，原样搬内核）
- GET  /ext/agent_manager/agents/{id}/config        读系统侧 agent（掩码 + 磁盘原文 etag；
                                                    admin 之外的写闸见 PUT）
- PUT  /ext/agent_manager/agents/{id}/config        写系统侧 agent → config/agents（语法校验
                                                    400 → If-Match 409 → .bak 备份 → 写回；
                                                    admin 由本插件自持检查）
- GET  /ext/agent_manager/agents/{mode_id}/{stem}/config   读模式包 agent（用户副本优先；
                                                    副本不存在回落出厂种子只读呈现
                                                    readonly:true, source:factory_seed）
- PUT  /ext/agent_manager/agents/{mode_id}/{stem}/config   写模式包 agent 用户副本
                                                    <USER_ROOT>/plugins/modes/mode_<mode>/
                                                    agents/<stem>.yaml（落盘 + 用户仓
                                                    git commit 尝试，失败 warn 不阻断；
                                                    越界 fail-closed；auth:user 用户门控）

服务（capabilities.services，供跨插件经 tool-executor 显式 plugin_id 调用）：
- agent.get             按 agent_id（系统两轮匹配或模式键 mode_X/<stem>）取解析后的 yaml dict
- agent.list            列表（双来源聚合；服务面无 caller token，模式注册表缺席时注明）
- agent.config-validate yaml 语法校验（不写盘）

行为契约（照 kernel/crates/api/src/routes.rs 现实现逐项搬移，一项不丢）：
- 两轮匹配：顶层 <id>.yaml 优先 → 递归文件名匹配 → config_id 回退（server.rs find_agent_yaml）
- agent_id 白名单 [A-Za-z0-9_-]（防路径穿越，routes.rs is_safe_agent_id）；模式键
  mode_X/<stem> 形态校验复用 plugins/shared/mode_keys.py（与 context_build 同口径）
- 掩码：key 含 api_key/apikey/secret/token/password → 值 "****"（${ENV} 占位符保留；
  config_service.rs mask_secrets）
- etag：磁盘原文 sha256 hex（B4 compute_etag）；PUT if_match 缺失/不匹配 → 409
  （模式包首写以出厂种子内容为乐观锁基线）
- 备份：写前 copy 为 <file>.yaml.bak（模式包首写建副本无备份，出厂种子即出厂回落）；
  非法 yaml → 400 不写盘
[来源: docs/decisions/2026-08-20-agent-manager-plugin.md;
 docs/working/模式体系落地设计_20260915.md §4.4]
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any

import yaml

from agentos_plugin_sdk import AgentOSPlugin
from agentos_plugin_sdk.bootstrap import bootstrap_plugin

logger = logging.getLogger(__name__)
plugin = AgentOSPlugin("agent_manager")

_paths = bootstrap_plugin(__file__)  # 插件目录 + plugins/shared 根（http_json）入 sys.path

from http_json import (  # noqa: E402
    decode_body as _decode_body,
    error as _error,
    json_response as _json_response,
    ok as _ok,
)
from atomic_io import atomic_write_text as _atomic_write_text  # noqa: E402
from kernel_token import decode_kernel_token as _decode_kernel_token  # noqa: E402
from mode_keys import parse_mode_agent_key as _parse_mode_agent_key  # noqa: E402
from user_space import user_plugins_dir as _user_plugins_dir  # noqa: E402
from user_space import user_root as _user_root

# ── 目录定位：AGENTOS_CONFIG_ROOT（内核启动写入，sidecar 继承进程环境）优先；
#    回退 __file__ 上溯项目根（与 context_build/task_form 同款防御）。──
_PLUGIN_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _PLUGIN_DIR.parents[3]  # plugins/shared/system/agent_manager → 仓库根

# 内核 0.2 开发期 token 格式（http/src/auth.rs）：base64_nopad("access:{user_id}:{username}:{exp}")
_BUILTIN_ADMIN_USER_ID = "00000000-0000-0000-0000-000000000001"


def _agents_dir() -> Path:
    """定位 config/agents 目录（内核 AGENTOS_CONFIG_ROOT=<root>/config）。"""
    root = os.environ.get("AGENTOS_CONFIG_ROOT", "")
    if root:
        return Path(root) / "agents"
    return _PROJECT_ROOT / "config" / "agents"


# ══ 模式包注册表（§4.4 聚合注册表的 mode 面）══
#
# 键形态 mode_X/<stem> 解析复用 mode_keys.parse_mode_agent_key（与 context_build/
# task_submit 同口径，stem 白名单天然杜绝路径穿越）。双根取数：
#   用户副本  <USER_ROOT>/plugins/modes/mode_<mode>/agents/<stem>.yaml（写面落点）
#   出厂种子  plugins/shared/modes/mode_<mode>/agents/<stem>.yaml（只读回落）
# 注册表真值 = 内核 /api/v1/schema 的 mode_agents（{key, plugin_id, path}，P2 透出）。


def _factory_modes_dir() -> Path:
    """出厂模式包根（plugins/shared/modes，bootstrap shared_root 单一真值）。"""
    return Path(_paths.shared_root) / "modes"


def _mode_factory_seed_path(mode: str, stem: str) -> Path:
    """出厂种子 agent yaml 路径（`mode` 为 mode_keys 语义的不带前缀模式名）。"""
    return _factory_modes_dir() / f"mode_{mode}" / "agents" / f"{stem}.yaml"


def _mode_user_copy_dir(mode: str) -> Path | None:
    """模式包用户副本 agents/ 目录；用户根不可得（极端环境）→ None。"""
    plugins_root = _user_plugins_dir()
    if plugins_root is None:
        return None
    return Path(plugins_root) / "modes" / f"mode_{mode}" / "agents"


def _mode_user_copy_path(mode: str, stem: str) -> Path | None:
    """用户副本 agent yaml 路径（不判存在；用户根不可得 → None）。"""
    agents_dir = _mode_user_copy_dir(mode)
    return None if agents_dir is None else agents_dir / f"{stem}.yaml"


def _resolve_mode_agent_source(agent_id: str) -> tuple[Path, str, bool] | None:
    """模式包 agent 读路径三元组（path, source, readonly）；不可读 → None。

    用户副本存在 → 可编辑（source=user_copy）；否则出厂种子 → 只读回落
    （source=factory_seed）；两者皆无（非模式键/文件不存在）→ None。
    """
    parsed = _parse_mode_agent_key(agent_id) if agent_id else None
    if parsed is None:
        return None
    mode, stem = parsed
    copy = _mode_user_copy_path(mode, stem)
    if copy is not None and copy.is_file():
        return copy, "user_copy", False
    seed = _mode_factory_seed_path(mode, stem)
    if seed.is_file():
        return seed, "factory_seed", True
    return None


# ══ 内核 schema 取数（模式包注册表真值面）══

_SCHEMA_TIMEOUT_SECONDS = 3.0  # 列表端点 timeout_ms=5000 内必须让路，超时即降级


def _kernel_base_url() -> str:
    """内核 HTTP 基址（AGENTOS_KERNEL_PORT，sidecar 继承内核进程环境；缺省 9100）。"""
    port = (os.environ.get("AGENTOS_KERNEL_PORT") or "").strip() or "9100"
    return f"http://localhost:{port}"


def _bearer_token(headers: dict[str, str] | None) -> str | None:
    """从转发头提取 caller bearer token（内核 dispatcher 原样透传 Authorization）。"""
    for k, v in (headers or {}).items():
        if isinstance(k, str) and k.lower() == "authorization" and isinstance(v, str):
            bare = v[7:] if v[:7].lower() == "bearer " else v
            return bare or None
    return None


def fetch_mode_agents(token: str | None) -> tuple[list[dict[str, Any]], str | None]:
    """GET /api/v1/schema 取 mode_agents（{key, plugin_id, path}）。

    返回 (entries, error)：error 为 None = 取数成功（entries 可为空表）；
    非 None = 取数失败原因（调用方降级仅系统表并注明）。无 caller token 时
    不发请求（schema 需已认证读角色）。内核未透出该字段（旧版本 additive
    缺席）按空注册表处理，不算降级。
    """
    if not token:
        return [], "no caller token: /api/v1/schema requires authentication"
    req = urllib.request.Request(
        f"{_kernel_base_url()}/api/v1/schema",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=_SCHEMA_TIMEOUT_SECONDS) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (OSError, ValueError) as exc:  # URLError/HTTPError/超时 ⊆ OSError；JSON ⊆ ValueError
        logger.warning(
            "[agent_manager] /api/v1/schema 取数失败（降级仅系统表）| error=%s", exc
        )
        return [], f"schema fetch failed: {exc}"
    entries = payload.get("mode_agents") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        return [], None
    return [e for e in entries if isinstance(e, dict)], None


# ══ 定位与扫描（routes.rs collect_yaml_files / resolve_agent_yaml_path +
#    server.rs find_agent_yaml 语义）══


def is_safe_agent_id(agent_id: str) -> bool:
    """agent_id 白名单（字母/数字/`-`/`_`）——防 percent-decode 后路径穿越。"""
    return bool(agent_id) and all(c.isascii() and (c.isalnum() or c in "-_") for c in agent_id)


def collect_yaml_files(dir_path: Path) -> list[Path]:
    """递归收集 .yaml/.yml（按路径排序保证稳定）。"""
    if not dir_path.is_dir():
        return []
    files = [p for p in dir_path.rglob("*") if p.suffix in (".yaml", ".yml") and p.is_file()]
    return sorted(files)


def find_agent_yaml(dir_path: Path, agent_id: str) -> Path | None:
    """两轮匹配：文件名 <agent_id>.yaml → yaml 内 config_id（递归）。"""
    target = f"{agent_id}.yaml"
    fallback: list[Path] = []
    for p in sorted(dir_path.iterdir()) if dir_path.is_dir() else []:
        if p.is_dir():
            found = find_agent_yaml(p, agent_id)
            if found is not None:
                return found
        elif p.name == target:
            return p
        elif p.suffix == ".yaml":
            fallback.append(p)
    for p in fallback:
        try:
            raw = p.read_text(encoding="utf-8")
            cfg = yaml.safe_load(raw)
        except (OSError, yaml.YAMLError):
            continue
        if isinstance(cfg, dict) and cfg.get("config_id") == agent_id:
            return p
    return None


def resolve_agent_yaml_path(agent_id: str) -> Path | None:
    """顶层 config/agents/<id>.yaml 优先，再递归分类子目录；非法 id 一律 None。"""
    if not is_safe_agent_id(agent_id):
        return None
    agents_dir = _agents_dir()
    top = agents_dir / f"{agent_id}.yaml"
    if top.is_file():
        return top
    return find_agent_yaml(agents_dir, agent_id)


# ══ 掩码与 etag（config_service.rs mask_secrets / compute_etag 语义）══

_SECRET_KEY_MARKERS = ("api_key", "apikey", "secret", "token", "password")


def _is_secret_key(key: str) -> bool:
    lower = key.lower()
    return any(marker in lower for marker in _SECRET_KEY_MARKERS)


def _mask_secret_value(value: Any) -> Any:
    """`${ENV}` 占位符原样，真实明文 → `****`（非字符串原样保留）。"""
    if not isinstance(value, str):
        return value
    if value.startswith("${") and value.endswith("}"):
        return value
    return "****"


def mask_secrets(value: Any) -> Any:
    """递归掩码敏感字段值（secret 命中后其子树不再递归——对齐内核实现）。"""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if isinstance(k, str) and _is_secret_key(k):
                out[k] = _mask_secret_value(v)
            else:
                out[k] = mask_secrets(v)
        return out
    if isinstance(value, list):
        return [mask_secrets(v) for v in value]
    return value


def compute_etag(raw: str) -> str:
    """磁盘原文 sha256 hex（弱校验语义，B4）。"""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ══ 业务语义（routes.rs agents_handler / get / put 逐项对齐）══


def _agent_list_item(
    parsed: dict[str, Any], fallback_id: str, item_id: str | None = None
) -> dict[str, Any] | None:
    """yaml dict → 列表条目（config_id/name 身份字段语义单点，系统/模式两源共用）。

    config_id 链 = yaml config_id → yaml id → fallback_id（模式面 fallback 为键
    stem，注册键权威）；item_id 覆盖寻址 id（模式面为完整模式键）。
    """
    config_id = str(parsed.get("config_id") or parsed.get("id") or fallback_id)
    if not config_id:
        return None
    return {
        "id": item_id or config_id,
        "config_id": config_id,
        "name": str(parsed.get("name") or config_id),
        "description": str(parsed.get("description") or ""),
        "agent_type": str(parsed.get("agent_type") or ""),
        "status": "active",
        "model": str(parsed.get("model") or parsed.get("model_tier") or ""),
        "level": str(parsed.get("level") or ""),
        "model_tier": str(parsed.get("model_tier") or ""),
    }


def _agent_matches(item: dict[str, Any], agent_type: str | None, needle: str) -> bool:
    """agent_type 精确过滤 + name/config_id/description 大小写不敏感子串过滤。"""
    if agent_type is not None and item["agent_type"] != agent_type:
        return False
    if needle:
        haystack = (
            item["name"] + "\n" + item["config_id"] + "\n" + item["description"]
        ).lower()
        if needle not in haystack:
            return False
    return True


def list_agents(agent_type: str | None = None, search: str | None = None) -> dict[str, Any]:
    """扫描 config/agents/**/*.yaml → {items, total}（agent_type 过滤 + search 子串过滤）。

    search 对 name/config_id/description 做大小写不敏感子串匹配；空白串视为
    不过滤（对齐前端搜索框清空场景）。条目标 source: system。
    """
    needle = (search or "").strip().lower()
    items: list[dict[str, Any]] = []
    for path in collect_yaml_files(_agents_dir()):
        try:
            parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(parsed, dict):
            continue
        item = _agent_list_item(parsed, fallback_id="")
        if item is None or not _agent_matches(item, agent_type, needle):
            continue
        item["source"] = "system"
        items.append(item)
    return {"items": items, "total": len(items)}


def _mode_agent_items(
    entries: list[dict[str, Any]], agent_type: str | None, needle: str
) -> list[dict[str, Any]]:
    """schema mode_agents 注册表 → 列表条目（用户副本内容优先，坏条目跳过）。"""
    items: list[dict[str, Any]] = []
    for entry in entries:
        key = str(entry.get("key") or "")
        plugin_id = str(entry.get("plugin_id") or "")
        parsed_key = _parse_mode_agent_key(key) if key else None
        if parsed_key is None or not plugin_id:
            continue
        mode, stem = parsed_key
        copy = _mode_user_copy_path(mode, stem)
        raw_path = entry.get("path")
        seed = Path(str(raw_path)) if raw_path else _mode_factory_seed_path(mode, stem)
        source_file = copy if (copy is not None and copy.is_file()) else seed
        try:
            parsed = yaml.safe_load(source_file.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(parsed, dict):
            continue
        item = _agent_list_item(parsed, fallback_id=stem, item_id=key)
        if item is None or not _agent_matches(item, agent_type, needle):
            continue
        item["source"] = f"mode:{plugin_id}"
        items.append(item)
    return items


def aggregate_agents(
    agent_type: str | None = None,
    search: str | None = None,
    token: str | None = None,
) -> dict[str, Any]:
    """双来源聚合列表（§4.4）：系统注册表 + 模式包注册表。

    模式包条目经 /api/v1/schema 的 mode_agents 取数（caller token 复用内核
    鉴权）；取不到时降级仅系统表，经顶层 mode_registry 注明（available=false
    + error 原因），条目缺模式面不误报为全量。
    """
    base = list_agents(agent_type, search)
    entries, error = fetch_mode_agents(token)
    items = list(base["items"])
    if error is None:
        items.extend(_mode_agent_items(entries, agent_type, (search or "").strip().lower()))
    return {
        "items": items,
        "total": len(items),
        "mode_registry": {"available": error is None, "error": error},
    }


# 字段声明原样搬内核 agents_schema_handler（来源 .agent_template_spec.yaml）
AGENT_SCHEMA_FIELDS: list[dict[str, Any]] = [
    {"name": "config_id", "type": "string", "label": "配置ID", "required": True},
    {"name": "name", "type": "string", "label": "名称", "required": True},
    {"name": "display_name", "type": "string", "label": "显示名称"},
    {"name": "description", "type": "textarea", "label": "描述"},
    {
        "name": "agent_type",
        "type": "select",
        "label": "类型",
        "options": [
            {"label": "主控", "value": "main"},
            {"label": "编排", "value": "orchestrator"},
            {"label": "专用", "value": "specialized"},
            {"label": "原子", "value": "atomic"},
            {"label": "系统", "value": "system"},
        ],
    },
    {
        "name": "level",
        "type": "select",
        "label": "层级",
        "options": [
            {"label": "L1", "value": "L1"},
            {"label": "L2", "value": "L2"},
            {"label": "L3", "value": "L3"},
        ],
    },
    {"name": "model_tier", "type": "string", "label": "模型档位"},
    {"name": "system_prompt", "type": "textarea", "label": "系统提示词"},
    {"name": "tool_ids", "type": "multiselect", "label": "工具"},
    {"name": "max_iterations", "type": "number", "label": "最大迭代"},
    {"name": "timeout_seconds", "type": "number", "label": "超时秒"},
    {"name": "tags", "type": "multiselect", "label": "标签"},
]


def _read_config_payload(
    config_id: str, path: Path, *, source: str, readonly: bool
) -> tuple[int, dict[str, Any]]:
    """读 agent yaml 共用体（掩码 + 磁盘原文 etag + 来源/只读标注）。"""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        return 500, {"error": f"read agent config {path}: {exc}"}
    etag = compute_etag(raw)
    try:
        parsed = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        return 500, {"error": f"agent config yaml parse error: {exc}"}
    if parsed is None:
        parsed = {}
    masked = mask_secrets(parsed)
    yaml_text = yaml.safe_dump(masked, allow_unicode=True, sort_keys=True, default_flow_style=False)
    return 200, {
        "config_id": config_id,
        "yaml": yaml_text,
        "etag": etag,
        "source": source,
        "readonly": readonly,
    }


def get_agent_config(agent_id: str) -> tuple[int, dict[str, Any]]:
    """读 agent yaml：系统侧 config/agents；模式包用户副本优先、出厂种子只读回落。"""
    path = resolve_agent_yaml_path(agent_id)
    if path is not None:
        return _read_config_payload(agent_id, path, source="system", readonly=False)
    resolved = _resolve_mode_agent_source(agent_id)
    if resolved is None:
        return 404, {"error": f"agent config not found: {agent_id}"}
    mode_path, source, readonly = resolved
    return _read_config_payload(agent_id, mode_path, source=source, readonly=readonly)


def put_agent_config(agent_id: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """写回 agent yaml：语法校验 400 → If-Match 409 → .bak 备份 → 写回。

    系统侧写 config/agents（既有机制原样）；模式包 agent（键 mode_X/<stem>）
    写用户副本文件，出厂种子回落作首写乐观锁基线，越界 fail-closed。
    """
    new_yaml = body.get("yaml")
    if not isinstance(new_yaml, str):
        return 400, {"error": "missing required field: yaml"}
    if_match = body.get("if_match")

    path = resolve_agent_yaml_path(agent_id)
    if path is None:
        parsed = _parse_mode_agent_key(agent_id) if agent_id else None
        if parsed is None:
            return 404, {"error": f"agent config not found: {agent_id}"}
        return _put_mode_agent_config(agent_id, parsed[0], parsed[1], new_yaml, if_match)

    # 语法校验先行（T2）：解析失败一律 400 拒写，磁盘保持原值。
    try:
        yaml.safe_load(new_yaml)
    except yaml.YAMLError as exc:
        return 400, {"error": f"agent config yaml invalid: {exc}"}

    # If-Match 乐观锁（A13）：必须匹配磁盘当前 ETag（缺失/不匹配 → 409）。
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        return 500, {"error": f"read agent config {path}: {exc}"}
    current_etag = compute_etag(raw)
    if not (isinstance(if_match, str) and if_match == current_etag):
        return 409, {
            "error": f"ETag mismatch: current={current_etag}, given={if_match!r}"
        }

    # 先备份原文件（同目录，内核 with_extension("yaml.bak") 同构：<stem>.yaml.bak），
    # 再以原子替换写入新内容（write_text 直写有崩溃窗口 = agent 配置截断）。
    backup = path.with_suffix(".yaml.bak")
    try:
        backup.write_text(raw, encoding="utf-8")
        _atomic_write_text(path, new_yaml)
    except OSError as exc:
        return 500, {"error": f"write agent config {path}: {exc}"}
    return 200, {
        "config_id": agent_id,
        "success": True,
        "backup": backup.name,
        "etag": compute_etag(new_yaml),
    }


def _put_mode_agent_config(
    agent_id: str, mode: str, stem: str, new_yaml: str, if_match: Any
) -> tuple[int, dict[str, Any]]:
    """模式包 agent 写用户副本（<USER_ROOT>/plugins/modes/mode_<mode>/agents/）。

    用户副本已存在 → If-Match 对副本、.bak 备份副本；首写（副本不存在）→
    If-Match 对出厂种子内容（编辑器 GET 种子回落拿到的 etag），副本落盘无
    备份（出厂种子文件原样保留 = 出厂回落语义）。写盘成功后尝试用户仓
    git commit（<USER_ROOT>/.git 存在时；失败 warn 不阻断）。
    """
    copy = _mode_user_copy_path(mode, stem)
    if copy is None:
        return 500, {"error": "user root unavailable: mode agent user copy dir unresolved"}
    # agents_dir 与 copy 同源于 _user_plugins_dir()——copy 非 None 蕴含其非 None。
    agents_dir = _mode_user_copy_dir(mode)
    assert agents_dir is not None
    seed = _mode_factory_seed_path(mode, stem)
    if copy.is_file():
        current_path = copy
    elif seed.is_file():
        current_path = seed
    else:
        return 404, {"error": f"agent config not found: {agent_id}"}

    # 路径越界 fail-closed：写目标必须落在本模式包用户副本 agents/ 内
    #（解析器回归防御——键形态校验之外的第二道闸）。
    try:
        copy.resolve().relative_to(agents_dir.resolve())
    except (OSError, ValueError):
        return 400, {"error": f"write target escapes mode user copy dir: {agent_id}"}

    # 语法校验先行（T2，与系统侧一致）。
    try:
        yaml.safe_load(new_yaml)
    except yaml.YAMLError as exc:
        return 400, {"error": f"agent config yaml invalid: {exc}"}

    # If-Match 乐观锁：副本存在对副本，否则对出厂种子（首写基线）。
    try:
        raw = current_path.read_text(encoding="utf-8")
    except OSError as exc:
        return 500, {"error": f"read agent config {current_path}: {exc}"}
    current_etag = compute_etag(raw)
    if not (isinstance(if_match, str) and if_match == current_etag):
        return 409, {"error": f"ETag mismatch: current={current_etag}, given={if_match!r}"}

    backup: Path | None = None
    try:
        if copy.is_file():
            backup = copy.with_suffix(".yaml.bak")
            backup.write_text(raw, encoding="utf-8")
        else:
            copy.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(copy, new_yaml)
    except OSError as exc:
        return 500, {"error": f"write agent config {copy}: {exc}"}
    user_root = _user_root()
    if user_root is not None:
        _git_commit_user_copy(user_root, copy, agent_id)
    return 200, {
        "config_id": agent_id,
        "success": True,
        "backup": backup.name if backup is not None else None,
        "etag": compute_etag(new_yaml),
        "source": "user_copy",
        "readonly": False,
    }


def _git_commit_user_copy(user_root: Path, file_path: Path, agent_id: str) -> None:
    """用户仓 git 提交尝试（`<USER_ROOT>/.git` 存在时 add + commit）。

    git 不可用/非仓库/无变化/身份未配置/超时一律 warn 不阻断——文件落盘已
    成功，版本层失败不回滚写面（与 kernel 侧 user_space.rs git 提交助手
    并行开发中的语义对齐：UI 是编辑器，git 是同一份文件的版本层）。
    """
    if not (user_root / ".git").exists():
        return
    try:
        rel = file_path.resolve().relative_to(user_root.resolve()).as_posix()
    except (OSError, ValueError):
        logger.warning(
            "[agent_manager] 用户副本不在用户根内，跳过 git 提交 | agent=%s file=%s",
            agent_id, file_path,
        )
        return
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    for subcommand, args in (
        ("add", ["add", "--", rel]),
        ("commit", ["commit", "-m", f"agent_manager: 更新模式包 agent 用户副本 {agent_id}"]),
    ):
        try:
            proc = subprocess.run(
                ["git", "-C", str(user_root), *args],
                capture_output=True, text=True, errors="replace",
                timeout=8, env=env, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.warning(
                "[agent_manager] 用户仓 git %s 执行失败（不阻断）| agent=%s error=%s",
                subcommand, agent_id, exc,
            )
            return
        if proc.returncode != 0:
            logger.warning(
                "[agent_manager] 用户仓 git %s 未成功（不阻断）| agent=%s stdout=%s stderr=%s",
                subcommand, agent_id, proc.stdout.strip(), proc.stderr.strip(),
            )
            return


# ══ PUT 鉴权（内核 0.2 token 自持检查，write_surface_auth 的 /ext 等价）══


def _require_admin(headers: dict[str, str] | None) -> tuple[int, str] | None:
    """admin 闸：无效/缺失/过期 token → 401；有效但非 admin → 403。

    内核 write_surface_auth 不覆盖 /ext/**，本插件自持等价检查。角色判定按
    token 内 username/user_id（内核 token 无签名同水位；store 自定义 admin
    用户名不同时会误拒——0.2 单 admin 开发期已知限制，ADR 已记录）。
    """
    authz = ""
    for k, v in (headers or {}).items():
        if isinstance(k, str) and k.lower() == "authorization" and v:
            authz = str(v)
            break
    token = authz[7:] if authz.lower().startswith("bearer ") else ""
    if not token:
        return 401, "missing bearer token"
    decoded = _decode_kernel_token(token)
    if decoded is None:
        return 401, "invalid or expired token"
    user_id, username, exp = decoded
    if int(time.time()) >= exp:
        return 401, "invalid or expired token"
    if username != "admin" and user_id != _BUILTIN_ADMIN_USER_ID:
        return 403, "admin role required"
    return None


# ══ http.handle 分发（/ext/agent_manager/** 入口）══

_CONFIG_PATH_RE = re.compile(r"^/ext/agent_manager/agents/(?P<id>[^/]+)/config$")
# 模式包 agent 配置路由：{mode_id}/{stem} 两个单段参数组合寻址（键含 `/`，与
# 系统键在 URL 段数上不相交；manifest 侧为同形模板，寻址段数互斥无歧义）。
_MODE_CONFIG_PATH_RE = re.compile(
    r"^/ext/agent_manager/agents/(?P<mode>[^/]+)/(?P<stem>[^/]+)/config$"
)


@plugin.tool(
    name="http.handle",
    schema={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "method": {"type": "string"},
            "plugin_id": {"type": "string"},
            "raw_body": {"type": "string"},
            "headers": {"type": "object"},
            "query": {"type": "object"},
        },
    },
    description="HTTP endpoint handler for /ext/agent_manager/** (agent config management)",
)
async def http_handle(
    path: str = "",
    method: str = "GET",
    plugin_id: str = "",
    raw_body: str = "",
    headers: dict[str, str] | None = None,
    query: dict[str, str] | None = None,
) -> dict[str, Any]:
    """按 path 分发到 agent_manager 端点（语义对齐原内核 /api/v1/agents* + 模式面）。"""
    try:
        q = query or {}

        if path == "/ext/agent_manager/agents" and method == "GET":
            agent_type = q.get("agent_type") or None
            # 聚合读面：复用 caller bearer token 取模式包注册表（取不到降级注明）。
            return _ok(_json_response(
                aggregate_agents(agent_type, search=q.get("search"), token=_bearer_token(headers))
            ))

        if path == "/ext/agent_manager/agents/schema" and method == "GET":
            return _ok(_json_response({"fields": AGENT_SCHEMA_FIELDS}))

        m = _CONFIG_PATH_RE.match(path) if path else None
        mm = _MODE_CONFIG_PATH_RE.match(path) if path else None

        if m and method == "GET":
            status, payload = get_agent_config(m.group("id"))
            return _ok(_json_response(payload, status))

        if mm and method == "GET":
            mode_agent_id = f"{mm.group('mode')}/{mm.group('stem')}"
            status, payload = get_agent_config(mode_agent_id)
            return _ok(_json_response(payload, status))

        if m and method == "PUT":
            # admin 闸先行（内核 middleware 语义：鉴权先于 handler 一切判定）。
            denied = _require_admin(headers)
            if denied is not None:
                status, message = denied
                return _ok(_json_response({"error": message}, status))
            try:
                body = _decode_body(raw_body)
            except ValueError as exc:
                return _ok(_json_response({"error": str(exc)}, 400))
            status, payload = put_agent_config(m.group("id"), body)
            return _ok(_json_response(payload, status))

        if mm and method == "PUT":
            # 模式面用户门控：manifest auth:user（dispatcher 验签），插件侧不再设
            # admin 闸——用户副本是用户可写资产（§4.4 双面编辑：系统面 admin、
            # 模式面用户）。
            try:
                body = _decode_body(raw_body)
            except ValueError as exc:
                return _ok(_json_response({"error": str(exc)}, 400))
            mode_agent_id = f"{mm.group('mode')}/{mm.group('stem')}"
            status, payload = put_agent_config(mode_agent_id, body)
            return _ok(_json_response(payload, status))

        logger.warning("http.handle: no route for path=%s method=%s", path, method)
        return _ok(_json_response({"error": "not found", "path": path}, 404))
    except Exception as exc:  # noqa: BLE001
        logger.exception("agent_manager http.handle failed: %s", exc)
        return _error(f"agent_manager service error: {exc}", 500)


# ══ 服务面（capabilities.services 声明即契约，G2 校验声明=实现）══


@plugin.tool(
    name="agent.get",
    schema={
        "type": "object",
        "properties": {
            "agent_id": {
                "type": "string",
                "description": "Agent ID (yaml filename stem or config_id)",
            }
        },
        "required": ["agent_id"],
    },
    description="Load one agent config (parsed yaml dict) by agent_id (filename or config_id two-round match)",
)
async def agent_get(agent_id: str = "") -> dict[str, Any]:
    """按 agent_id 取解析后的 yaml dict（内部服务，不掩码——消费方为插件而非面板）。

    系统侧两轮匹配；模式键 mode_X/<stem> 用户副本优先、出厂种子回落。
    """
    path = resolve_agent_yaml_path(agent_id)
    if path is None:
        resolved = _resolve_mode_agent_source(agent_id)
        path = resolved[0] if resolved is not None else None
    if path is None:
        return {"found": False, "config": None}
    try:
        parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        # 配置存在但不可读/损坏：对外与 not-found 同形（契约不变），留痕供排查
        logger.warning("[agent_manager] agent 配置读取失败（按 not-found 返回）| agent_id=%s path=%s error=%s", agent_id, path, exc)
        return {"found": False, "config": None}
    if not isinstance(parsed, dict):
        parsed = {}
    return {"found": True, "config": parsed, "path": str(path)}


@plugin.tool(
    name="agent.list",
    schema={
        "type": "object",
        "properties": {
            "agent_type": {
                "type": "string",
                "description": "Optional filter by agent_type field (e.g. main/orchestrator/specialized/atomic/system)",
            }
        },
    },
    description="List agent configs from config/agents/**/*.yaml (optional agent_type filter)",
)
async def agent_list(agent_type: str = "") -> dict[str, Any]:
    """agent 列表（双来源聚合，同 HTTP 列表语义；agent_type 空串 = 不过滤）。

    服务面无 caller token，模式包注册表取不到时按降级注明（mode_registry）。
    """
    return aggregate_agents(agent_type or None, token=None)


@plugin.tool(
    name="agent.config-validate",
    schema={
        "type": "object",
        "properties": {
            "yaml": {
                "type": "string",
                "description": "Agent config yaml text to validate",
            }
        },
        "required": ["yaml"],
    },
    description="Validate agent config yaml syntax (parse check, no disk write)",
)
async def agent_config_validate(**kwargs: Any) -> dict[str, Any]:
    """yaml 语法校验（解析检查，不写盘）。

    参数名 ``yaml`` 与模块名冲突，经 ``**kwargs`` 接收（SDK 按签名过滤，
    VAR_KEYWORD 全量透传——agentos_plugin_sdk/server.py:_filter_handler_kwargs）。
    """
    yaml_text = kwargs.get("yaml")
    if not isinstance(yaml_text, str) or not yaml_text:
        return {"valid": False, "error": "missing required field: yaml"}
    try:
        yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        return {"valid": False, "error": f"agent config yaml invalid: {exc}"}
    return {"valid": True, "error": None}


@plugin.on_load
async def _on_load(_params: dict[str, Any]) -> None:
    logger.info(
        "agent_manager started (agents dir: %s, factory modes: %s)",
        _agents_dir(), _factory_modes_dir(),
    )


if __name__ == "__main__":
    plugin.run()
