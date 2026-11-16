"""执行通道挂载面 —— per-device mcp:external 插件目录生成（ADR D10'/D13'）。

mcp_local_host 模式泛化：远端设备能力经标准 mcp:external 插件挂为本端工具
（watcher 热发现装载，零内核改动）。安全契约（P6/D13'）：
- manifest 只携带 ``${ENV}`` 引用，token 明文永不落 manifest/注册表/同步面；
- 凭据两源供给：外部 env 预设（fail-fast 校验在场），或挂载动作自动签发
  （token_value 随挂载供给，经用户空间 .env 落盘——内核 connect 时
  ${ENV} overlay 解析同机制，错误在挂载点不在调用点）；
- 同名=同工具属地消歧由 manifest 的 ``mcp.allowed_roots`` 声明驱动（内核
  register_host/resolve_tool_instance 既有执法体），allowed_roots 空 = 不参与
  属地路由（安全缺省）。
"""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from typing import Any

from _root_api import atomic_write_text

_SAFE_DEVICE_ID = re.compile(r"[^A-Za-z0-9_.-]")
_MOUNT_DIR_PREFIX = "mcp_device_"
EXEC_TOKEN_ENV_PREFIX = "AGENTOS_EXEC_TOKEN_"


def sanitize_device_id(device_id: str) -> str:
    """设备 id → 目录/标识安全子集（其余字符转下划线）。"""
    return _SAFE_DEVICE_ID.sub("_", device_id)


def mount_plugin_id(device_id: str) -> str:
    """挂载插件 id：mcp_device_<清洗小写>（device_id 注册表主键唯一 → id 唯一）。"""
    return _MOUNT_DIR_PREFIX + sanitize_device_id(device_id).lower()


def exec_token_env(device_id: str) -> str:
    """该设备执行通道 token 的 env 名（per-device 双钥：与 sync token 分离，D11）。

    env 名走纯下划线变体（连字符/点不进 env 名，跨 shell 安全）。
    """
    return EXEC_TOKEN_ENV_PREFIX + re.sub(r"[^A-Za-z0-9]", "_", device_id).upper()


def protocol_handshake_error(declared: Any, *, minimum: str = "1") -> str | None:
    """挂载时协议版本执法（D10a'）：兼容 None；不兼容/畸形诚实错误文案。

    与 sync_agent/exec_channel.check_protocol_version 同契约（跨插件不共库，
    契约单源 = ADR D10a'）：未声明 = 不假拒（未知 ≠ 不兼容）；声明低于最低
    版或畸形 = 明确报错（禁静默降级）。
    """
    if declared is None or str(declared).strip() == "":
        return None
    declared_text = str(declared).strip()
    try:
        declared_major = int(declared_text.split(".")[0])
        minimum_major = int(str(minimum).split(".")[0])
    except (ValueError, AttributeError):
        return (
            f"exec_channel.protocol_version 畸形（declared={declared_text!r}，"
            f"minimum={minimum}），拒绝挂载"
        )
    if declared_major < minimum_major:
        return (
            f"exec_channel.protocol_version 不兼容（declared={declared_text} < 最低 {minimum}），"
            "明确报错不静默降级"
        )
    return None


def build_mount_manifest(
    record: dict[str, Any], *, token_env: str, token_value: str | None = None
) -> dict[str, Any]:
    """routing_view 设备记录 → mcp:external 挂载 manifest。

    fail-fast：exec_channel/mcp_url 缺失、凭据缺失均当场明确报错；凭据两源
    ——外部 env 预设（os.environ 在场校验）或挂载自动签发（token_value 供给，
    随后落用户空间 .env 供内核 connect 时解析）。manifest 内 auth.value =
    ``${token_env}`` 引用（明文不落盘）。
    """
    device_id = str(record.get("device_id", "") or "")
    if not device_id:
        raise ValueError("设备记录缺 device_id，无法挂载")
    if not token_env:
        raise ValueError("token_env 必填（per-device 执行凭据的 env 引用名）")
    if token_value is None and token_env not in os.environ:
        raise ValueError(
            f"环境变量 {token_env} 未设置（执行通道 per-device token 缺失，挂载拒绝 fail-fast）"
        )
    caps = record.get("capabilities") or {}
    channel = caps.get("exec_channel") or {}
    mcp_url = str(channel.get("mcp_url", "") or "")
    if not mcp_url:
        raise ValueError(f"设备 {device_id} 未上报 exec_channel.mcp_url（执行通道不可挂载）")

    mcp: dict[str, Any] = {
        "transport": "streamable_http",
        "endpoint": {
            "url": mcp_url,
            "auth": {"type": "bearer", "value": f"${{{token_env}}}", "required": True},
        },
    }
    allowed_roots = channel.get("allowed_roots") or []
    if allowed_roots:
        mcp["allowed_roots"] = [str(r) for r in allowed_roots]
    return {
        "id": mount_plugin_id(device_id),
        "name": f"Device mount: {device_id}",
        "description": (
            f"2c 执行通道挂载（设备 {device_id} 属地算力经 mcp:external；"
            "ADR 2026-10-17-unified-path-tree-and-capability-plane D10'）"
        ),
        "version": "1.0.0",
        "plugin_type": "tool",
        "language": "external",
        "host_type": "sidecar",
        "entry": "mcp:external",
        "capabilities": {},
        "mcp": mcp,
        "permissions": {},
        "priority": 30,
    }


def provision_exec_token_env(env_path: Path, env_name: str, token: str) -> Path:
    """凭据体供给用户空间 .env（行级合并，内核 write_env_updates 同语义）。

    既有键原行更新（重签轮换不留旧值副本）、新键追加、注释与空行保留；
    原子写（同目录 tmp + replace，配置截断窗口零暴露）。内核侧
    resolve_env_placeholders 每次现读 .env——写完即对下次 connect 生效，
    无需重启。
    """
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.is_file() else []
    prefix = f"{env_name}="
    last_idx: int | None = None
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped == env_name or stripped.startswith(prefix):
            last_idx = i
    rendered = (
        f'"{token}"' if (" " in token or "#" in token or "\t" in token) else token
    )
    if last_idx is not None:
        lines[last_idx] = prefix + rendered
    else:
        lines.append(prefix + rendered)
    env_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(env_path, "\n".join(lines) + "\n")
    return env_path


def write_mount(plugins_root: Path, manifest: dict[str, Any]) -> Path:
    """挂载目录落盘（幂等重写）；watcher 热发现自动装载。"""
    target = Path(plugins_root) / str(manifest["id"])
    target.mkdir(parents=True, exist_ok=True)
    (target / "plugin.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return target


def remove_mount(plugins_root: Path, device_id: str) -> bool:
    """卸挂载（目录守卫：只动 mcp_device_* 命名空间）。返回是否确有删除。"""
    target = Path(plugins_root) / mount_plugin_id(device_id)
    if not target.is_dir() or not target.name.startswith(_MOUNT_DIR_PREFIX):
        return False
    shutil.rmtree(target)
    return True
