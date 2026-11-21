"""回顾工作流事件层：把工具调用落成步骤节点（一次回顾 = 一条工作流实例）。

职责边界：
- 只做步骤事件的写与读（单文件原子写：tmp → ``os.replace``）；不做业务判定、
  不算指标、不写账本（``eval_state.json`` 的读写仍归 ``server.py``）。
- 步骤枚举的唯一真源是包内 ``rules/flow_steps.yaml``（B 部类物料）——step 名
  不在表内即**拒写 + warn**，不静默落野值。
- 事件由 ``server.py`` 的工具 handler 在业务动作之后触发（服务端埋点）；不靠
  agent 提示词自觉上报（上报必漂移，且等于自证）。

存储布局：``reports/eval/workflows/<wf_id>.json``（与 ``<run_id>/``、
``proposals/``、``trajectory_library/`` 同级）。整体目录可用环境变量
``AGENTOS_EVAL_WORKFLOW_DIR`` 覆盖（测试面：把落盘重定向到 tmp_path）。

失效语义（best-effort，不阻断业务）：任何写盘/读盘异常一律 warn 后返回空值，
工具照常返回成功——埋点失败不能让评测动作失败。
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

import yaml

_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.abspath(os.path.join(_PLUGIN_DIR, "..", "..", ".."))
_DEFAULT_WF_DIR = os.path.join(_PROJECT_ROOT, "reports", "eval", "workflows")
_FLOW_STEPS_PATH = os.path.join(_PLUGIN_DIR, "rules", "flow_steps.yaml")

ENV_WF_DIR = "AGENTOS_EVAL_WORKFLOW_DIR"
# 无管道上下文时的活动工作流指针（进程外兜底：连续工具调用落在同一工作流）
_POINTER_NAME = "_current.json"
_POINTER_TTL_S = 6 * 3600
_KEEP_WORKFLOWS = 200

_running_node = ("running", "done", "failed", "skipped", "unavailable")


def _log() -> Any:
    """延迟取 logger（插件未初始化日志时退回标准库，避免导入期副作用）。"""
    import logging

    return logging.getLogger("mode_evolution.workflow_ledger")


def wf_dir() -> str:
    """工作流目录：env 覆盖优先（测试/隔离面），缺省 reports/eval/workflows。"""
    return (os.environ.get(ENV_WF_DIR, "").strip() or _DEFAULT_WF_DIR)


def _wf_path(wf_id: str) -> str:
    return os.path.join(wf_dir(), f"{wf_id}.json")


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _now_tag() -> str:
    return time.strftime("%Y%m%d_%H%M%S")


def _atomic_write(path: str, payload: dict[str, Any]) -> None:
    """整文件原子写：同目录 tmp → ``os.replace``（读者永不见半截 JSON）。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".wf-", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=1)
        Path(tmp).replace(target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ── 步骤枚举（rules/flow_steps.yaml 的唯一消费点）──────────────────────────

_catalog_cache: dict[str, Any] = {"key": "", "data": None}


def _load_flow_decl() -> dict[str, Any]:
    """读 rules/flow_steps.yaml（按 mtime 失效重载；读失败返回空声明不抛）。"""
    try:
        stat = os.stat(_FLOW_STEPS_PATH)
        key = f"{_FLOW_STEPS_PATH}:{stat.st_mtime_ns}"
    except OSError:
        return {}
    if _catalog_cache["key"] == key and isinstance(_catalog_cache["data"], dict):
        return _catalog_cache["data"]
    try:
        with open(_FLOW_STEPS_PATH, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except (OSError, yaml.YAMLError) as exc:
        _log().warning("[workflow] flow_steps 读取失败（步骤枚举降级为空）: %s", exc)
        return {}
    if not isinstance(data, dict):
        return {}
    _catalog_cache["key"] = key
    _catalog_cache["data"] = data
    return data


def step_catalog() -> list[dict[str, Any]]:
    """步骤定义列表（step/title/upstream/downstream/is_gate/drives）。"""
    steps = _load_flow_decl().get("steps") or []
    out: list[dict[str, Any]] = []
    for item in steps:
        if not isinstance(item, dict) or not str(item.get("step") or ""):
            continue
        out.append({
            "step": str(item.get("step")),
            "title": str(item.get("title") or ""),
            "upstream": [str(x) for x in (item.get("upstream") or [])],
            "downstream": [str(x) for x in (item.get("downstream") or [])],
            "is_gate": bool(item.get("is_gate")),
            "drives": [str(x) for x in (item.get("drives") or [])],
        })
    return out


def flow_edges() -> list[dict[str, Any]]:
    """回边声明（from/to/kind/label）：面板按 kind=back 画虚线。"""
    edges = _load_flow_decl().get("edges") or []
    out: list[dict[str, Any]] = []
    for item in edges:
        if not isinstance(item, dict):
            continue
        out.append({"from": str(item.get("from") or ""),
                    "to": str(item.get("to") or ""),
                    "kind": str(item.get("kind") or "forward"),
                    "label": str(item.get("label") or "")})
    return out


def step_ids() -> list[str]:
    """合法步骤名集合（顺序即声明顺序）。"""
    return [d["step"] for d in step_catalog()]


def reject_step(category: str) -> str:
    """proposal(action='reject') 的 category → 归属步骤（未声明回落 T2 归因）。"""
    mapping = _load_flow_decl().get("reject_steps") or {}
    if isinstance(mapping, dict) and str(category or "") in mapping:
        return str(mapping[str(category)])
    return "T2"


def close_reasons() -> list[str]:
    reasons = _load_flow_decl().get("close_reasons") or []
    return [str(x) for x in reasons] or ["unknown"]


# ── 工作流身份解析 ─────────────────────────────────────────────────────

def _ambient_pipeline_id() -> str:
    """当前执行的管道 id（内核 _log_ctx 经 SDK 日志上下文注入；取不到返回空串）。"""
    try:
        from agentos_plugin_sdk.logging.context import LogContext

        value = str(LogContext.get("pipeline_id") or "").strip()
    except Exception:  # noqa: BLE001 —— 无 SDK/无上下文都归「无管道身份」
        return ""
    return "" if value in ("", "-") else value


def _read_pointer() -> str:
    path = os.path.join(wf_dir(), _POINTER_NAME)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return ""
    if not isinstance(data, dict):
        return ""
    try:
        touched = float(data.get("touched_at") or 0)
    except (TypeError, ValueError):
        return ""
    if time.time() - touched > _POINTER_TTL_S:
        return ""
    return str(data.get("wf_id") or "")


def _write_pointer(wf_id: str) -> None:
    try:
        _atomic_write(os.path.join(wf_dir(), _POINTER_NAME),
                      {"wf_id": wf_id, "touched_at": time.time()})
    except OSError as exc:
        _log().warning("[workflow] 活动指针写入失败 wf_id=%s: %s", wf_id, exc)


def resolve_wf_id(explicit: str = "") -> str:
    """解析当前工作流 id（已关闭的自动轮转到 ``<base>-rN``，不复活旧实例）。

    优先级：显式 wf_id → 当前管道 id → 活动指针（6h 内）→ 新 ``wf_<时间戳>``。
    返回的 id 可能尚无对应文件（调用方写入即创建）。
    """
    if explicit:
        return explicit
    base = _ambient_pipeline_id() or _read_pointer() or f"wf_{_now_tag()}"
    candidate = base
    round_no = 1
    while True:
        doc = load_workflow(candidate)
        if doc is None or str(doc.get("status") or "running") == "running":
            return candidate
        round_no += 1
        candidate = f"{base}-r{round_no}"


# ── 工作流读写 ─────────────────────────────────────────────────────────

def load_workflow(wf_id: str) -> dict[str, Any] | None:
    """读单个工作流；缺失/损坏返回 None（不抛，供页不阻断）。"""
    if not wf_id:
        return None
    try:
        with open(_wf_path(wf_id), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def list_workflows(limit: int = 20) -> list[dict[str, Any]]:
    """按 opened_at 升序返回最近 limit 条（超限截断保留最新；目录缺失返回空）。"""
    try:
        names = [p.name for p in Path(wf_dir()).iterdir()
                 if p.name.endswith(".json") and not p.name.startswith("_")]
    except OSError:
        return []
    directory = wf_dir()
    paths = sorted((os.path.join(directory, n) for n in names),
                   key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0)
    if limit > 0:
        paths = paths[-limit:]
    out: list[dict[str, Any]] = []
    for path in paths:
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            out.append(data)
    out.sort(key=lambda d: str(d.get("opened_at") or ""))
    return out


def find_open_workflow() -> dict[str, Any] | None:
    """最新一条 running 工作流（面板「本次回顾」聚焦用）；无则 None。"""
    for doc in reversed(list_workflows(limit=_KEEP_WORKFLOWS)):
        if str(doc.get("status") or "running") == "running":
            return doc
    return None


def open_workflow(mode: str = "", round_type: str = "treatment",
                  wf_id: str = "", pipeline_id: str = "") -> str:
    """开工作流（已存在则原样复用，不重置节点）；返回 wf_id。"""
    wf_id = wf_id or resolve_wf_id()
    doc = load_workflow(wf_id)
    if doc is not None:
        return wf_id
    payload = {
        "wf_id": wf_id,
        "mode": str(mode or ""),
        "round_type": str(round_type or "treatment"),
        "status": "running",
        "close_reason": "",
        "opened_at": _now(),
        "closed_at": "",
        "pipeline_id": str(pipeline_id or _ambient_pipeline_id()),
        "steps": [],
    }
    try:
        _atomic_write(_wf_path(wf_id), payload)
    except OSError as exc:
        _log().warning("[workflow] open 失败 wf_id=%s mode=%s: %s", wf_id, mode, exc)
        return ""
    _write_pointer(wf_id)
    _prune()
    return wf_id


def _prune() -> None:
    """保留最新 _KEEP_WORKFLOWS 个工作流（与 rounds[-200:] 同口径）。"""
    try:
        names = [p.name for p in Path(wf_dir()).iterdir()
                 if p.name.endswith(".json") and not p.name.startswith("_")]
    except OSError:
        return
    directory = wf_dir()
    if len(names) <= _KEEP_WORKFLOWS:
        return
    paths = sorted((os.path.join(directory, n) for n in names),
                   key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0)
    for path in paths[: len(paths) - _KEEP_WORKFLOWS]:
        try:
            os.remove(path)
        except OSError as exc:
            _log().warning("[workflow] 旧工作流清理失败 %s: %s", path, exc)


def _save(doc: dict[str, Any]) -> bool:
    wf_id = str(doc.get("wf_id") or "")
    try:
        _atomic_write(_wf_path(wf_id), doc)
    except OSError as exc:
        _log().warning("[workflow] 写盘失败 wf_id=%s step=%s: %s",
                       wf_id, (doc.get("steps") or [{}])[-1].get("step", ""), exc)
        return False
    _write_pointer(wf_id)
    return True


def _ensure(wf_id: str, mode: str = "", round_type: str = "") -> dict[str, Any] | None:
    """取/建工作流；创建失败返回 None（调用方 warn 后放行）。"""
    doc = load_workflow(wf_id)
    if doc is not None:
        return doc
    opened = open_workflow(mode=mode, round_type=round_type or "treatment", wf_id=wf_id)
    if not opened:
        return None
    return load_workflow(opened)


def _seq_of(doc: dict[str, Any]) -> int:
    steps = doc.get("steps") or []
    return int(max((int(s.get("seq") or 0) for s in steps), default=0))


def begin_step(wf_id: str, step: str, inputs: dict[str, Any] | None = None,
               driven_by: str = "tool", mode: str = "",
               round_type: str = "") -> dict[str, Any]:
    """登记步骤开始；返回 ``{wf_id, seq, started_at}``（失败返回 ``{}``）。"""
    if step not in step_ids():
        _log().warning("[workflow] 未知步骤拒写 step=%r wf_id=%s（合法: %s）",
                       step, wf_id, step_ids())
        return {}
    wf_id = resolve_wf_id(wf_id)
    doc = _ensure(wf_id, mode=mode, round_type=round_type)
    if doc is None:
        return {}
    seq = _seq_of(doc) + 1
    node = {"seq": seq, "step": step, "status": "running",
            "started_at": _now(), "ended_at": "",
            "inputs": dict(inputs or {}), "artifacts": {},
            "driven_by": str(driven_by or "tool")}
    doc.setdefault("steps", []).append(node)
    if mode and not doc.get("mode"):
        doc["mode"] = str(mode)
    if round_type:
        doc["round_type"] = str(round_type)
    if not _save(doc):
        return {}
    return {"wf_id": wf_id, "seq": seq, "started_at": node["started_at"]}


def end_step(wf_id: str, seq: int, status: str = "done",
             artifacts: dict[str, Any] | None = None) -> dict[str, Any]:
    """登记步骤结束（幂等：已结束的节点返回首值，不覆写状态与时间）。"""
    if status not in _running_node:
        status = "done"
    doc = load_workflow(wf_id)
    if doc is None:
        _log().warning("[workflow] end_step 找不到工作流 wf_id=%s seq=%s（不造幽灵工作流）",
                       wf_id, seq)
        return {}
    for node in doc.get("steps") or []:
        if int(node.get("seq") or 0) != int(seq):
            continue
        if node.get("status") != "running":
            return {"wf_id": wf_id, "seq": seq, "status": node.get("status"),
                    "ended_at": node.get("ended_at", ""), "idempotent": True}
        node["status"] = status
        node["ended_at"] = _now()
        if artifacts:
            merged = dict(node.get("artifacts") or {})
            merged.update(artifacts)
            node["artifacts"] = merged
        if not _save(doc):
            return {}
        return {"wf_id": wf_id, "seq": seq, "status": status,
                "ended_at": node["ended_at"]}
    _log().warning("[workflow] end_step seq 不存在 wf_id=%s seq=%s", wf_id, seq)
    return {}


def finish_step(wf_id: str, step: str, status: str = "done",
                artifacts: dict[str, Any] | None = None,
                inputs: dict[str, Any] | None = None,
                driven_by: str = "tool", mode: str = "",
                round_type: str = "") -> dict[str, Any]:
    """结束该步骤最近一个 running 节点；无 running 节点则补一条已完成节点。

    补节点是「步骤确实发生过」的证据（例如直接 apply 未先 submit），不是伪游标：
    时间戳取事件发生的真实时刻，起止同秒是事实而非补造。
    """
    wf_id = resolve_wf_id(wf_id)
    doc = _ensure(wf_id, mode=mode, round_type=round_type)
    if doc is None:
        return {}
    target = None
    for node in doc.get("steps") or []:
        if node.get("step") == step and node.get("status") == "running":
            target = node
    if target is not None:
        return end_step(wf_id, int(target.get("seq") or 0), status=status,
                        artifacts=artifacts)
    begun = begin_step(wf_id, step, inputs=inputs, driven_by=driven_by,
                       mode=mode, round_type=round_type)
    if not begun:
        return {}
    return end_step(wf_id, int(begun["seq"]), status=status, artifacts=artifacts)


def close_workflow(wf_id: str, reason: str = "unknown") -> dict[str, Any]:
    """关闭工作流（reason 取 flow_steps.yaml 的 close_reasons；未知值落 unknown）。"""
    wf_id = resolve_wf_id(wf_id)
    doc = load_workflow(wf_id)
    if doc is None:
        return {}
    if str(doc.get("status") or "running") != "running":
        return {"wf_id": wf_id, "status": doc.get("status"),
                "close_reason": doc.get("close_reason", ""), "idempotent": True}
    doc["status"] = "closed"
    doc["close_reason"] = reason if reason in close_reasons() else "unknown"
    doc["closed_at"] = _now()
    if not _save(doc):
        return {}
    return {"wf_id": wf_id, "status": "closed", "close_reason": doc["close_reason"],
            "closed_at": doc["closed_at"]}


def current_round_id(wf_id: str = "") -> str:
    """当前工作流最近一轮的 run_id（提案 round_id 软关联键）；无则空串。"""
    doc = load_workflow(resolve_wf_id(wf_id))
    if doc is None:
        return ""
    for node in reversed(doc.get("steps") or []):
        run_id = str((node.get("artifacts") or {}).get("run_id") or "")
        if run_id:
            return run_id
    return ""
