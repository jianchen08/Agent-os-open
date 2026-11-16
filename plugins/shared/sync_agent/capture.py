"""DB 逻辑记录通道捕获面 —— 注册元数据 + 捕获源适配纯函数（T1，两通道架构）。

架构依据：ADR ``docs/decisions/2026-10-17-sync-two-channel-routing.md`` D3——
DB 通道 = 通用逻辑记录形态（源表名+主键+行快照+HLC），capture/apply/镜像
存储全通用；session/message/task 只是 DB 通道上的**三个注册项**，差异只收进
注册元数据（捕获源/键构造/呈现维度），不写三份通道代码。

本模块只做无副作用变换：

- **注册元数据**（:data:`DB_REGISTRATIONS`）：每注册项声明源表名与键构造/
  载荷构造，通道引擎（agent_core ``_enqueue_record``）与镜像 apply 按
  ``sync_event.asset_channel`` 路由，不感知具体类型。
- **消息指纹** = 规范 JSON（去 ``seq`` 引擎槽位元数据与 ``_`` 前缀引擎私有键
  后 sha256）——与内核 message_id"规范化 hash 排除 seq/_前缀字段"同精神
  （engine/store/schema.rs message_slots 不变量）。指纹即消息身份：
  ``asset_id = {pipeline_id}/{指纹}``，压缩/重生成 = 新指纹新对象（旧值留
  账本可审计），insert 顺延 seq 漂移不影响身份。
- **任务快照** = state 中 task.* 扁平键集的子集投影；指纹漂移即任务态变迁
  （每变迁一事件，任务属主终态权威，上位 ADR D2）。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

__all__ = [
    "DB_REGISTRATIONS",
    "TASK_STATE_KEYS",
    "canonical_message",
    "fingerprint_bytes",
    "fingerprint_message",
    "session_record",
    "task_snapshot",
    "task_snapshot_fingerprint",
    "message_record",
]


def canonical_message(msg: Any) -> dict[str, Any]:
    """消息对象的捕获规范形：去 ``seq``（引擎槽位元数据）与 ``_`` 前缀私有键。

    非对象输入返回空对象（捕获面宽容，畸形消息零抛出——上游已按对象过滤，
    此处是纯函数级的最后防线）。
    """
    if not isinstance(msg, dict):
        return {}
    return {
        k: v
        for k, v in msg.items()
        if k != "seq" and not (isinstance(k, str) and k.startswith("_"))
    }


def fingerprint_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fingerprint_message(msg: Any) -> str:
    """消息捕获指纹：canonical_message 后紧凑 JSON 的 sha256（十六进制）。"""
    return fingerprint_bytes(
        json.dumps(canonical_message(msg), ensure_ascii=False, sort_keys=True).encode("utf-8")
    )


# ── DB 通道载荷构造（注册元数据的载荷面；通道引擎只认此形态）──────


def message_record(
    pipeline_id: str, seq: int, msg: dict[str, Any], fingerprint: str
) -> tuple[str, dict[str, Any]]:
    """消息注册项：``(asset_id, payload)``。键 = {pipeline}/{内容指纹}，
    呈现维度 = (pipeline_id, seq)（改写按此收敛单行，呈现零重复）。"""
    asset_id = f"{pipeline_id}/{fingerprint}"
    payload = {
        "table": "messages",
        "pk": asset_id,
        "row": canonical_message(msg),
        "tombstone": False,
        "view": {"group": pipeline_id, "seq": seq},
    }
    return asset_id, payload


def session_record(
    thread_id: str, *, tombstone: bool, origin: str
) -> tuple[str, dict[str, Any]]:
    """会话注册项：``(asset_id, payload)``。键 = thread_id（墓碑同键追加）。"""
    asset_id = thread_id
    payload = {
        "table": "sessions",
        "pk": thread_id,
        "row": {"thread_id": thread_id, "origin": origin},
        "tombstone": bool(tombstone),
    }
    return asset_id, payload


# ── 任务域捕获源适配 ─────────────────────────────────────────

#: 任务域扁平键（捕获步 state.reads 同名单；task_birth 出生契约键集）。
TASK_STATE_KEYS: tuple[str, ...] = (
    "task.id",
    "task.status",
    "task.goal",
    "task.description",
    "task.acceptance_criteria",
    "task.created_at",
    "task.dependencies",
    "task.submitted_by",
    "task.submitted_by_level",
)


def task_snapshot(state: dict[str, Any]) -> dict[str, Any]:
    """state → task.* 扁平键子集投影（无任务域 = 空快照）。"""
    return {k: state[k] for k in TASK_STATE_KEYS if k in state}


def task_snapshot_fingerprint(snapshot: dict[str, Any]) -> str:
    """任务快照指纹（漂移判据：指纹变 = 任务态变迁 = 捕获一事件）。"""
    return fingerprint_bytes(
        json.dumps(snapshot, ensure_ascii=False, sort_keys=True).encode("utf-8")
    )


def task_record(pipeline_id: str, snapshot: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """任务注册项：``(asset_id, payload)``。键 = pipeline_id（= task.id），
    属主终态权威——每漂移一事件，同键追加。"""
    asset_id = pipeline_id
    payload = {
        "table": "tasks",
        "pk": pipeline_id,
        "row": dict(snapshot),
        "tombstone": False,
    }
    return asset_id, payload


#: DB 通道注册表（路由中间件的 agent 侧视图；通道判定在 sync_event.asset_channel）。
#: 元数据 = 源表名 + 载荷构造函数引用（键构造内含其中）；捕获源（域事件/管道
#: 步骤）由装配面接线，通道代码不按类型增殖。
DB_REGISTRATIONS: dict[str, str] = {
    "session": "sessions",
    "message": "messages",
    "task": "tasks",
}
