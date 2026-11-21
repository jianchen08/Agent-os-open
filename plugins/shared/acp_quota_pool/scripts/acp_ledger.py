"""ACP 额度账本 v0 —— 被动记账面（只记派发，不做主动查询）。

落点按 user_root 约定（``<USER_ROOT>/data/acp_bridge/quota_ledger.json``；
本模块自带 user-root 解析，与 ``plugins/shared/user_space.py`` 及 Rust 侧
``kernel/crates/core/src/user_space.rs`` 同一套规则——任何一侧改解析规则
三处必须同步）。运行态数据：丢失可重建、可接受（M0 裁定）。

每 provider 记：派发计数 / 最近会话 / 时间线（有界）/ 撞墙信号
（quota_or_rate_limit、auth 两类计墙）。账本词汇（墙的判定）住本模块
——记账权威；分类逻辑住 acp_engine。文件损坏 ValueError 拒载
（fail-closed，不静默重置丢账）；写入同目录 tmp + os.replace 原子换入。
"""

# config-write-surface-exempt: 运行态账本落用户空间 data 面（user_root 解析与
# user_space.py / kernel core::user_space 同源单套规则），非 config/.env 配置面；
# 可丢可重建的 M0 运行数据，非插件自拼路径直写配置。

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

__all__ = [
    "TIMELINE_CAP",
    "WALL_CLASSES",
    "USER_ROOT_ENV",
    "USER_DATA_DIR_ENV",
    "QuotaLedger",
    "default_ledger_path",
]

TIMELINE_CAP = 50
WALL_CLASSES = frozenset({"quota_or_rate_limit", "auth"})
_VERSION = 1

USER_ROOT_ENV = "AGENTOS_USER_ROOT"
USER_DATA_DIR_ENV = "AGENTOS_DATA_DIR"

_Clock = Callable[[], float]


def _env_path(name: str) -> Path | None:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return None
    return Path(raw.strip())


def _user_data_dir() -> Path | None:
    """用户数据根：``AGENTOS_DATA_DIR`` > ``<AGENTOS_USER_ROOT>/data``。

    **零回落**（ADR 2026-11-07-plugin-tiered-single-copy-model §2.2，对齐
    ``plugins/shared/user_space.py`` 本体——用户根未设 = None，不做 OS 标准
    目录推导；``%APPDATA%\\agentos`` 回落 = dev 扫到装机目录陈旧副本的事故
    机制根，本体已删）。本模块随技能载荷播种到 ``<user_root>/skills/``，
    播种副本对共享根裸模块不可达（禁共享根跨包 import 铁律）——自包含副本
    合法存在，同输入同输出由对齐断言钉死
    （``acp_quota_pool/tests/test_acp_ledger.py::TestUserSpaceCopyAlignment``），
    改动须与本体同刀。
    """
    data_dir = _env_path(USER_DATA_DIR_ENV)
    if data_dir is not None:
        return data_dir
    user_root = _env_path(USER_ROOT_ENV)
    return user_root / "data" if user_root is not None else None


def default_ledger_path() -> Path:
    """账本落点：``<user_data>/acp_bridge/quota_ledger.json``（user_root 约定）。

    用户空间不可得（极端环境）时 RuntimeError——账本无处可落是配置错误，
    不静默降级丢账。
    """
    data_dir = _user_data_dir()
    if data_dir is None:
        raise RuntimeError(
            f"用户空间不可得（{USER_ROOT_ENV}/{USER_DATA_DIR_ENV} 均未设——零回落，无 OS 目录兜底）"
            "——额度账本无处落盘"
        )
    return data_dir / "acp_bridge" / "quota_ledger.json"


def _atomic_write_text(path: Path, text: str) -> None:
    """同目录 tmp 写入 + replace 原子换入（Windows 目标被占短退避重试）。"""
    tmp_path = path.with_name(path.name + ".tmp")
    try:
        tmp_path.write_text(text, encoding="utf-8")
        for attempt in range(3):
            try:
                tmp_path.replace(path)
                return
            except OSError:
                if attempt == 2:
                    raise
                time.sleep(0.05)
    finally:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass


class QuotaLedger:
    """per-provider 记账面（文件持久化，读-改-原子写）。"""

    def __init__(self, path: Path | str, clock: _Clock | None = None) -> None:
        self._path = Path(path)
        self._clock: _Clock = clock or time.time
        self._state: dict[str, Any] = self._load()

    def _load(self) -> dict[str, Any]:
        if not self._path.exists():
            return {"version": _VERSION, "providers": {}}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"额度账本损坏（{self._path}）: {exc}") from exc
        if not isinstance(raw, dict) or not isinstance(raw.get("providers"), dict):
            raise ValueError(f"额度账本形态非法（{self._path}）：providers 必须是映射")
        return raw

    def _provider(self, provider_id: str) -> dict[str, Any]:
        return self._state["providers"].setdefault(
            provider_id,
            {
                "dispatch_total": 0,
                "ok_total": 0,
                "wall_hit_total": 0,
                "last_session_id": "",
                "last_dispatch_at": "",
                "last_status": "",
                "timeline": [],
                "wall_hits": [],
            },
        )

    def _timestamp(self) -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(self._clock()))

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(self._path, json.dumps(self._state, ensure_ascii=False, indent=1))

    def record_success(self, provider_id: str, session_id: str, summary: str = "") -> None:
        state = self._provider(provider_id)
        state["dispatch_total"] += 1
        state["ok_total"] += 1
        state["last_session_id"] = session_id
        state["last_dispatch_at"] = self._timestamp()
        state["last_status"] = "ok"
        state["timeline"].append(
            {
                "ts": state["last_dispatch_at"],
                "status": "ok",
                "session_id": session_id,
                "error_class": "",
                "message": summary[:500],
            }
        )
        del state["timeline"][:-TIMELINE_CAP]
        self._save()

    def record_failure(self, provider_id: str, error_class: str, message: str = "") -> None:
        state = self._provider(provider_id)
        state["dispatch_total"] += 1
        state["last_dispatch_at"] = self._timestamp()
        state["last_status"] = "failed"
        state["timeline"].append(
            {
                "ts": state["last_dispatch_at"],
                "status": "failed",
                "session_id": "",
                "error_class": error_class,
                "message": message[:500],
            }
        )
        del state["timeline"][:-TIMELINE_CAP]
        if error_class in WALL_CLASSES:
            state["wall_hit_total"] += 1
            state["wall_hits"].append(
                {"ts": state["last_dispatch_at"], "error_class": error_class, "message": message[:500]}
            )
            del state["wall_hits"][:-TIMELINE_CAP]
        self._save()

    def report(self) -> list[dict[str, Any]]:
        """per-provider 报表行（按 provider id 排序；账本视图的拷贝）。"""
        rows: list[dict[str, Any]] = []
        for pid in sorted(self._state["providers"]):
            state = self._state["providers"][pid]
            wall_hits = state["wall_hits"]
            rows.append(
                {
                    "provider": pid,
                    "dispatch_total": state["dispatch_total"],
                    "ok_total": state["ok_total"],
                    "wall_hit": state["wall_hit_total"] > 0,
                    "wall_hit_total": state["wall_hit_total"],
                    "last_session_id": state["last_session_id"],
                    "last_dispatch_at": state["last_dispatch_at"],
                    "last_status": state["last_status"],
                    "last_wall_hit": dict(wall_hits[-1]) if wall_hits else {},
                    "timeline": [dict(entry) for entry in state["timeline"]],
                }
            )
        return rows
