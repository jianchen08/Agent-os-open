#!/usr/bin/env python3
"""任务派发脚本：向主 agent 会话投递任务简报（自主批次编排用）。

链路 = 登录取 token →（可选）POST /api/v1/sessions 建线程 → POST /api/v1/ws-ticket
→ WS /ws/chat?ticket= 发 user_input，等 stream_end 或超时（超时只放弃等待，
管道在服务端继续跑）。契约对齐：frontend/src/services/auth/wsTicket.ts、
api/auth.ts、api/session.ts（createSession 带 X-Main-Agent-Request: true）、
GlobalWebSocket.sendUserInput。口令从 AGENTOS_ADMIN_PASSWORD 读取，
未设即硬失败（内核无默认口令——首启随机播种，硬编码回落无合法场景）。

用法（用 .venv/Scripts/python.exe 跑，依赖 websockets）：
  python scripts/dispatch_task.py --list
  python scripts/dispatch_task.py --create "标题"
  python scripts/dispatch_task.py --thread <id> --file a.md b.md
  python scripts/dispatch_task.py --thread <id> --text "一句话任务"
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

BASE = os.environ.get("AGENTOS_KERNEL_URL", "http://127.0.0.1:9100")

# 与前端 createSession 相同的标记头（session.ts）；缺省内核按普通请求处理
MAIN_AGENT_HEADER = {"X-Main-Agent-Request": "true"}


def _request(path: str, payload: dict | None, token: str | None = None,
             method: str = "POST", timeout: int = 20) -> dict:
    data = json.dumps(payload or {}).encode("utf-8")
    headers = {"Content-Type": "application/json", **MAIN_AGENT_HEADER}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def load_password() -> str:
    pw = os.environ.get("AGENTOS_ADMIN_PASSWORD")
    if pw:
        return pw
    sys.exit(
        "[error] AGENTOS_ADMIN_PASSWORD 未设：内核口令无默认值（首启随机播种），"
        "请显式提供（口令见内核首次启动日志或运维侧设定）。"
    )


def list_threads(token: str) -> None:
    data = _request("/api/v1/sessions", None, token, method="GET")
    threads = data.get("threads") or data.get("data") or []
    for t in threads:
        print(f"{t.get('thread_id')}  {t.get('intent') or t.get('title') or ''}")


def create_thread(token: str, title: str) -> str:
    data = _request("/api/v1/sessions", {"title": title, "intent": title}, token)
    thread_id = data.get("thread_id") or (data.get("data") or {}).get("thread_id")
    if not thread_id:
        raise SystemExit(f"[err] 建线程响应无 thread_id: {json.dumps(data)[:300]}")
    print(f"[ok] thread_id={thread_id}")
    return thread_id


TERMINAL_TASK_STATES = {"completed", "failed", "cancelled", "stopped", "timeout"}


def check_state(token: str, pipeline_id: str) -> str:
    """状态门（用户裁定：每步动作前必查，终态即停）。

    权威来源 = 任务服务 GET /ext/task_service/tasks/{id}（有任务检查插件，
    不手搓 DB 查询）。task.status 永卡 running 是已知收口缺陷（D2），故
    叠加 run 终态交叉验证：task 终态 或 run 终态 任一成立即 TERMINAL。
    返回判定：TERMINAL（禁止 nudge/resume，需重派走新任务）/ ACTIONABLE。

    注意（设计口径，用户裁定 2026-09-14）：chat 会话型主管道在任务服务中
    无条目是**设计本身**（任务检测/评估闸门管任务域，会话管道归会话生命周期），
    此处 task 查询 404 属预期——会话管道以 run 状态为权威。
    """
    task_status, run_status = "?", "?"
    try:
        data = _request(f"/ext/task_service/tasks/{pipeline_id}", None, token, method="GET")
        d = data.get("data") or data
        task_status = str(d.get("status") or "?")
    except Exception as exc:
        task_status = f"查询失败({exc.__class__.__name__})"
    try:
        import sqlite3

        conn = sqlite3.connect(
            f"file:{Path(__file__).resolve().parent.parent / 'agentos_kernel.db'}?mode=ro", uri=True
        )
        row = conn.execute(
            "SELECT status FROM runs WHERE pipeline_id=? ORDER BY created_at DESC LIMIT 1",
            (pipeline_id,),
        ).fetchone()
        run_status = row[0] if row else "no-run"
    except Exception as exc:
        run_status = f"查询失败({exc.__class__.__name__})"
    print(f"[gate] {pipeline_id} task={task_status} run={run_status}")
    if task_status in TERMINAL_TASK_STATES or run_status in TERMINAL_TASK_STATES | {"failed"}:
        print("[gate] 判定=TERMINAL：禁止 nudge/resume（僵尸状态）；继续推进请重派新任务")
        return "TERMINAL"
    print("[gate] 判定=ACTIONABLE")
    return "ACTIONABLE"


def dispatch(token: str, thread_id: str, text: str, wait_seconds: int) -> None:
    from websockets.sync.client import connect

    ticket = _request("/api/v1/ws-ticket", {}, token)["ticket"]
    ws_url = BASE.replace("http", "ws", 1) + f"/ws/chat?ticket={ticket}"
    message = {
        "type": "user_input",
        "thread_id": thread_id,
        "content": text,
        "pipeline_id": "",
        "attachments": [],
        "enable_thinking": False,
        "thinking_strength": "",
        "client_message_id": f"zcode-{int(time.time() * 1000)}",
    }
    with connect(ws_url, open_timeout=15) as ws:
        ws.send(json.dumps(message, ensure_ascii=False))
        print(f"[sent] thread={thread_id} 字数={len(text)}")
        deadline = time.time() + wait_seconds
        while True:
            remain = deadline - time.time()
            if remain <= 0:
                print("[info] 等待超时，管道在服务端继续运行")
                break
            try:
                raw = ws.recv(timeout=remain)
            except TimeoutError:
                print("[info] 等待超时，管道在服务端继续运行")
                break
            try:
                frame = json.loads(raw)
            except (TypeError, ValueError):
                continue
            ftype = frame.get("type", "?")
            if ftype == "stream_end":
                print("[ok] stream_end（主 agent 本轮已收尾）")
                break
            if ftype in ("stream_error", "error"):
                print(f"[err] {json.dumps(frame, ensure_ascii=False)[:400]}")
                break
            if ftype in ("run_started", "run_status", "thread_state"):
                print(f"[frame] {ftype}: {json.dumps(frame, ensure_ascii=False)[:200]}")


def dispatch_root_task(token: str, title: str, description: str, target_id: str,
                       ac_marker: str, thread_id: str,
                       ws_path: str = "", ws_mode: str = "") -> None:
    """直派根任务（绕过 L1 会话链）：POST /ext/task_service/tasks/root。

    契约对齐 eval_harness/batch_run.py 与 tasks/http_api.py TaskRootCreate
    （L2/L3，系统硬规则禁 L1）。写仓库类任务必须带 ws_path=仓库根 +
    ws_mode=plain——缺省空工作区使文件工具被围栏在 .ai_workspaces/{id}，
    任务无法读仓改仓（2026-09-13 批次 v2 教训）。
    AC 统一 file_check：任务工作空间 report.md 含 ac_marker。
    """
    payload = {
        "title": title,
        "description": description,
        "target_id": target_id,
        "acceptance_criteria": {
            "file_check": {"input_params": {"path": "report.md", "check": "contains",
                                            "pattern": ac_marker}},
        },
        "thread_id": thread_id,
        "workspace": ws_path,
        "workspace_mode": ws_mode,
    }
    resp = _request("/ext/task_service/tasks/root", payload, token, timeout=30)
    task_id = str(resp.get("id") or (resp.get("data") or {}).get("id") or "")
    print(f"[ok] task_id={task_id} target={target_id} ws={ws_path or '(default)'}")


def ws_drive_task(token: str, title: str, description: str, target_id: str,
                  thread_id: str | None, ws_path: str, ws_mode: str,
                  wait_seconds: int, steer: bool = True) -> str:
    """WS 直驱：建会话 → PATCH 绑定执行者 → user_input 带 execution_context。

    背景：直派与会话链都存在"首轮纯文本应答即终局"假绿（1 次 llm_call 后
    post ended:true，实锤见 docs/working/batch_20260913/D2 简报）——steer
    前导强制首轮必须调工具，压该概率。
    """
    if steer:
        description = (
            "【执行纪律（最高优先，先读这里）】本会话是任务执行会话，不是问答："
            "你的第一轮回复就必须调用工具（推荐先 list_directory 看仓库结构），"
            "之后每轮持续调用工具推进，直到产出交付物并 commit。"
            "纯文本应答会立即触发管道终局——那等于任务失败。\n\n"
            + description
        )
    if thread_id:
        print(f"[ws] 复用线程 {thread_id}")
    else:
        data = _request("/api/v1/sessions", {"title": title, "intent": title}, token)
        thread_id = data.get("thread_id") or (data.get("data") or {}).get("thread_id")
        print(f"[ws] 建会话 {thread_id}")
    _request(f"/api/v1/sessions/{thread_id}/agent", {"agent_id": target_id}, token,
             method="PATCH")
    print(f"[ws] 绑定执行者 {target_id}")

    from websockets.sync.client import connect

    ticket = _request("/api/v1/ws-ticket", {}, token)["ticket"]
    ws_url = BASE.replace("http", "ws", 1) + f"/ws/chat?ticket={ticket}"
    message = {
        "type": "user_input",
        "thread_id": thread_id,
        "content": description,
        "pipeline_id": "",
        "attachments": [],
        "enable_thinking": False,
        "thinking_strength": "",
        "client_message_id": f"zcode-{int(time.time() * 1000)}",
    }
    if ws_path:
        message["execution_context"] = {"workspace": {"source_path": ws_path,
                                                      "mode": ws_mode or "plain"}}
    with connect(ws_url, open_timeout=15) as ws:
        ws.send(json.dumps(message, ensure_ascii=False))
        print(f"[ws] 已投递（{len(description)} 字），等 {wait_seconds}s 观察受理帧")
        deadline = time.time() + wait_seconds
        saw_ack = False
        while time.time() < deadline:
            try:
                raw = ws.recv(timeout=max(1, deadline - time.time()))
            except TimeoutError:
                break
            try:
                frame = json.loads(raw)
            except (TypeError, ValueError):
                continue
            ftype = frame.get("type", "")
            if ftype in ("new_message", "run_started", "reasoning_delta", "text_delta"):
                saw_ack = True
                print(f"[ws] 受理确认帧: {ftype}")
                break
        if not saw_ack:
            print("[ws] 未观测到受理帧（管道可能仍在服务端排队/执行）")
    return thread_id


def main() -> None:
    parser = argparse.ArgumentParser(description="向主 agent 会话派发任务简报")
    parser.add_argument("--list", action="store_true", help="列出现有会话线程")
    parser.add_argument("--create", metavar="TITLE", help="新建会话线程")
    parser.add_argument("--thread", metavar="ID", help="目标会话线程 id")
    parser.add_argument("--file", nargs="+", help="简报文件（UTF-8），依序派发")
    parser.add_argument("--text", help="直接派发一段文本")
    parser.add_argument("--wait-seconds", type=int, default=120, help="每条消息等 stream_end 的秒数")
    parser.add_argument("--task-file", metavar="PATH", help="直派模式：简报文件 → 根任务（不经 L1 会话）")
    parser.add_argument("--target", help="直派模式：目标执行者 agent id（L2/L3）")
    parser.add_argument("--ac-marker", default="DONE", help="直派模式：AC 检查 report.md 含此标记")
    parser.add_argument("--task-thread", help="直派模式：任务挂靠 thread_id（合成 id 即可）")
    parser.add_argument("--ws-path", default="", help="直派模式：工作空间 source_path（写仓任务=仓库根）")
    parser.add_argument("--ws-mode", default="", help="直派模式：worktree/plain（写仓任务=plain）")
    parser.add_argument("--ws-drive", action="store_true",
                        help="WS 直驱模式：建会话+PATCH 绑执行者+user_input（绕开 tasks/root 直派缺陷）")
    parser.add_argument("--check", metavar="PIPELINE_ID",
                        help="状态门：查 task/run 状态并给 TERMINAL/ACTIONABLE 判定（动作前必过）")
    args = parser.parse_args()

    try:
        token = _request("/api/v1/auth/login",
                         {"username": "admin", "password": load_password()})
    except urllib.error.URLError as exc:
        raise SystemExit(f"[err] 内核不可达（{BASE}）：{exc.reason}\n"
                         "内核可能在重启/停止中——状态门与派发须等内核健康后执行")
    token = token["access_token"]

    if args.check:
        check_state(token, args.check)
        return
    if args.list:
        list_threads(token)
        return
    if args.create:
        create_thread(token, args.create)
        return

    if args.task_file:
        if not args.target:
            raise SystemExit("[err] 需要 --target")
        title = Path(args.task_file).stem
        desc = Path(args.task_file).read_text(encoding="utf-8")
        if args.ws_drive:
            ws_drive_task(token, title, desc, args.target, args.task_thread,
                          args.ws_path, args.ws_mode, args.wait_seconds)
        else:
            if not args.task_thread:
                raise SystemExit("[err] 直派模式需要 --task-thread")
            dispatch_root_task(token, title, desc,
                               args.target, args.ac_marker, args.task_thread,
                               ws_path=args.ws_path, ws_mode=args.ws_mode)
        return

    if not args.thread:
        raise SystemExit("[err] 需要 --thread（或先用 --create 建线程）")
    texts: list[str] = []
    if args.file:
        for f in args.file:
            texts.append(Path(f).read_text(encoding="utf-8"))
    if args.text:
        texts.append(args.text)
    if not texts:
        raise SystemExit("[err] 需要 --file 或 --text")

    for i, text in enumerate(texts, 1):
        print(f"=== [{i}/{len(texts)}] ===")
        dispatch(token, args.thread, text, args.wait_seconds)
        time.sleep(3)


if __name__ == "__main__":
    main()
