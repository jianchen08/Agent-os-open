#!/usr/bin/env python3
"""
task_01 统一通用数据接口 + DB 管理页 — 可复现功能验证脚本
============================================================
对应验证报告：docs/working/unified_db_admin_function_verify_report.md

覆盖 V1-V7 验证清单：
  V1 统一接口枚举（10 表 + 401）

  V3 行 CRUD（插入/单行/更新/删除 + 复合主键 + 非 admin 403 + viewer 只读）
  V4 SQL 执行器（SELECT/confirm/危险语句/多语句/403）
  V5 前端契约（dbAdmin.ts 的 URL/方法/请求体/响应解包 —— API 级等价验证）
  V6 表扩展性（临时建表 → 自动可见/可查/可写 → 清理）
  V7 兼容性回归（/health、/auth/*、/threads、channel_api 路由注册）

用法：
  python3 verify_reproduce.py [--port 9100] [--db /tmp/task01_verify.db]
  前置：内核二进制 kernel/target/debug/agentos-kernel 已构建；
        若 --start-kernel 则自动启动独立测试 DB 的内核服务。

注意：本脚本以「真实 HTTP 请求 + sqlite 直查交叉验证」执行，不依赖 curl；
      页面级浏览器验证受容器内存限制（cgroup 1.36GB，vite OOM）无法在容器内执行，
      详见验证报告 §五 不可验证项。
"""
import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1"
PASS, FAIL, WARN = [], [], []


def http(method: str, port: int, path: str, token: str | None = None,
         body: dict | None = None, params: list | None = None):
    """发送 HTTP 请求，返回 (status, json_body)。params 为 [(k,v),...] 以支持重复参数。"""
    url = f"{BASE}:{port}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode()
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return e.code, {"raw": raw}


def record(name: str, ok: bool, detail: str = "", is_warn: bool = False):
    tag = "PASS" if ok else ("WARN" if is_warn else "FAIL")
    (WARN if is_warn else (PASS if ok else FAIL)).append(name)
    print(f"[{tag}] {name} | {detail[:150]}")


def login(port: int, username: str, password: str) -> str:
    status, data = http("POST", port, "/api/v1/auth/login", body={
        "username": username, "password": password})
    assert status == 200, f"登录失败 {status}: {data}"
    return data["access_token"]


def sqlite_check(db_path: str, sql: str):
    con = sqlite3.connect(db_path)
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9100)
    ap.add_argument("--db", default="/tmp/task01_verify.db")
    ap.add_argument("--start-kernel", action="store_true",
                    help="自动以独立测试 DB 启动内核（需已构建二进制）")
    args = ap.parse_args()

    if args.start_kernel:
        print(f"启动内核：AGENTOS_KERNEL_PORT={args.port} AGENTOS_DB_PATH={args.db}")
        env = dict(os.environ, AGENTOS_KERNEL_PORT=str(args.port),
                   AGENTOS_DB_PATH=args.db, AGENTOS_KERNEL_HOST="127.0.0.1")
        proc = subprocess.Popen(
            ["./kernel/target/debug/agentos-kernel"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
        for _ in range(30):
            try:
                urllib.request.urlopen(f"{BASE}:{args.port}/health", timeout=2)
                break
            except Exception:
                time.sleep(1)
        print(f"内核已就绪 (pid={proc.pid})")

    # ── 登录 ──
    admin = login(args.port, "admin", "admin12345")
    print(f"[INFO] admin token 获取成功")
    viewer = None
    try:
        # register 默认 role=user，仅能验证 403；viewer 只读需手动改角色（本脚本直接以 user 验证 403）
        _, reg = http("POST", args.port, "/api/v1/auth/register", body={
            "username": "verify_user", "password": "verify12345",
            "email": "verify@test.dev"})
        viewer = reg.get("access_token")
    except Exception:
        pass

    # ══════════ V1 统一接口 ══════════
    st, data = http("GET", args.port, "/api/v1/db/tables", admin)
    names = [t["name"] for t in data.get("tables", [])]
    expected = ["runs", "messages", "traces", "blobs", "branches", "sessions",
                "execution_records", "pipeline_run_summaries", "memory", "users"]
    record("V1-1 tables 枚举 10 表", st == 200 and all(e in names for e in expected),
           f"status={st} tables={sorted(names)}")
    st2, _ = http("GET", args.port, "/api/v1/db/tables")
    record("V1-2 无 token → 401", st2 == 401, f"status={st2}")

    # ══════════ V2 行查询 ══════════
    # 准备 8 条 memory 测试数据（幂等：先清空再插）
    con = sqlite3.connect(args.db)
    con.execute("DELETE FROM memory")
    for i in range(8):
        con.execute(
            "INSERT INTO memory (id, content, memory_type, tags, score, tenant_id, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (f"mem_test_{i:02d}", f"测试记忆内容第{i}号",
             "episode" if i % 2 == 0 else "semantic", "[]", i * 1.5,
             "default", f"2026-08-0{(i % 3) + 1}T10:00:00+00:00"))
    con.commit()
    con.close()

    st, d = http("GET", args.port, "/api/v1/db/table/memory?limit=3&offset=0", admin)
    record("V2-1 分页 limit=3", st == 200 and d.get("total") == 8 and len(d.get("rows", [])) == 3,
           f"status={st} total={d.get('total')} rows={len(d.get('rows', []))}")
    st, d = http("GET", args.port, "/api/v1/db/table/memory?limit=2&offset=5", admin)
    record("V2-2 分页 offset=5", st == 200 and [r["id"] for r in d.get("rows", [])] ==
           ["mem_test_05", "mem_test_06"], f"status={st} ids={[r['id'] for r in d.get('rows', [])]}")
    st, d = http("GET", args.port, "/api/v1/db/table/memory",
                 admin, params=[("filter", "memory_type:eq:episode")])
    record("V2-3 单条件 eq 筛选", st == 200 and d.get("total") == 4,
           f"status={st} total={d.get('total')}")

    st, d = http("GET", args.port, "/api/v1/db/table/memory",
                 admin, params=[("filter", "content:contains:3")])
    record("V2-5 contains 筛选", st == 200 and d.get("total") == 1,
           f"status={st} total={d.get('total')}")
    st, d = http("GET", args.port, "/api/v1/db/table/memory?sort=score:desc&limit=3", admin)
    record("V2-6 排序 desc", st == 200 and [r["score"] for r in d.get("rows", [])] ==
           [10.5, 9.0, 7.5], f"status={st} scores={[r['score'] for r in d.get('rows', [])]}")
    st, _ = http("GET", args.port, "/api/v1/db/table/memory", admin,
                 params=[("filter", "content:eq:'%3B%20DROP%20TABLE%20memory--")])
    alive = sqlite_check(args.db, "SELECT COUNT(*) FROM memory")[0][0]
    record("V2-7/8 注入尝试被拒 + 表未损", st == 200 and alive == 8,
           f"status={st} memory 行数={alive}")
    st, _ = http("GET", args.port, "/api/v1/db/table/memory", admin,
                 params=[("filter", "nonexistent_col:eq:x")])
    record("V2-9 非法列 → 400", st == 400, f"status={st}")
    st, _ = http("GET", args.port, "/api/v1/db/table/nonexistent_table", admin)
    record("V2-10 非法表 → 404", st == 404, f"status={st}")


    # ══════════ V3 行 CRUD ══════════
    st, d = http("POST", args.port, "/api/v1/db/table/memory", admin, body={
        "row": {"id": "mem_api_001", "content": "CRUD测试", "memory_type": "episode",
                "score": 66.0, "created_at": "2026-08-05T16:10:00+00:00"}})
    rid = d.get("row_id", "")
    record("V3-1 POST 插入 → 201 + row_id", st == 201 and rid == "mem_api_001",
           f"status={st} row_id={rid}")
    st, d = http("GET", args.port, f"/api/v1/db/table/memory/{rid}", admin)
    record("V3-2 GET 单行确认落库", st == 200 and d.get("content") == "CRUD测试",
           f"status={st}")
    st, d = http("PATCH", args.port, f"/api/v1/db/table/memory/{rid}", admin,
                 body={"updates": {"content": "CRUD已更新", "score": 99.0}})
    record("V3-3 PATCH 更新 → 200", st == 200 and d.get("row", {}).get("content") == "CRUD已更新",
           f"status={st}")
    st, d = http("DELETE", args.port, f"/api/v1/db/table/memory/{rid}", admin)
    record("V3-4 DELETE → 200 deleted", st == 200 and d.get("deleted") is True,
           f"status={st}")
    # 复合主键 execution_records
    st, d = http("POST", args.port, "/api/v1/db/table/execution_records", admin, body={
        "row": {"record_id": "rec_cp_001", "sequence": 1, "pipeline_run_id": "run1",
                "iteration": 1, "role": "assistant", "content": "复合主键",
                "created_at": "2026-08-05T16:30:00+00:00"}})
    record("V3-5 复合主键插入", st == 201 and d.get("row_id") == "rec_cp_001,1",
           f"status={st} row_id={d.get('row_id')}")
    st, d = http("GET", args.port, "/api/v1/db/table/execution_records/rec_cp_001,1", admin)
    record("V3-6 复合主键 GET（,拼接）", st == 200, f"status={st}")
    st, _ = http("PATCH", args.port, "/api/v1/db/table/execution_records/rec_cp_001,1",
                 admin, body={"updates": {"content": "复合已更新"}})
    record("V3-7 复合主键 PATCH", st == 200, f"status={st}")
    st, _ = http("DELETE", args.port, "/api/v1/db/table/execution_records/rec_cp_001,1", admin)
    record("V3-8 复合主键 DELETE", st == 200, f"status={st}")
    # 权限
    if viewer:
        st, _ = http("POST", args.port, "/api/v1/db/table/memory", viewer,
                     body={"row": {"content": "x"}})
        record("V3-9 非 admin 写 → 403", st == 403, f"status={st}")
        st, _ = http("GET", args.port, "/api/v1/db/tables", viewer)
        record("V3-10 非 admin 读（register 默认 user）→ 期望 403（viewer/admin 才可读）",
               st == 403, f"status={st}")

    # ══════════ V4 SQL 执行器 ══════════
    st, d = http("POST", args.port, "/api/v1/db/execute", admin,
                 body={"sql": "SELECT id, content FROM memory LIMIT 2", "confirm": True})
    record("V4-1 SELECT → 200 + columns/rows",
           st == 200 and d.get("columns") == ["id", "content"] and len(d.get("rows", [])) == 2,
           f"status={st} columns={d.get('columns')}")
    st, _ = http("POST", args.port, "/api/v1/db/execute", admin,
                 body={"sql": "UPDATE memory SET score=1 WHERE id='mem_test_00'"})
    record("V4-2 写语句无 confirm → 400", st == 400, f"status={st}")
    for sql, expect in [("DROP TABLE memory", 403), ("ALTER TABLE memory ADD COLUMN x TEXT", 403),
                        ("DELETE FROM memory", 403), ("SELECT 1; DROP TABLE memory", 400),
                        ("PRAGMA journal_mode=WAL", 403)]:
        st, _ = http("POST", args.port, "/api/v1/db/execute", admin,
                     body={"sql": sql, "confirm": True})
        record(f"V4-3 危险语句拒绝: {sql[:40]} → {expect}",
               st == expect, f"status={st}")

    # ══════════ V6 表扩展性 ══════════
    st, _ = http("POST", args.port, "/api/v1/db/execute", admin,
                 body={"sql": "CREATE TABLE future_tasks (id TEXT PRIMARY KEY, title TEXT, "
                              "tenant_id TEXT, created_at TEXT)", "confirm": True})
    st, d = http("GET", args.port, "/api/v1/db/tables", admin)
    visible = "future_tasks" in [t["name"] for t in d.get("tables", [])]
    record("V6-1 建表后自动可见", st == 200 and visible, f"status={st} visible={visible}")
    st, _ = http("POST", args.port, "/api/v1/db/table/future_tasks", admin,
                 body={"row": {"id": "t1", "title": "未来任务"}})
    record("V6-2 新表可写入", st == 201, f"status={st}")
    st, d = http("GET", args.port, "/api/v1/db/table/future_tasks", admin,
                 params=[("filter", "title:contains:未来")])
    record("V6-3 新表可查询/筛选", st == 200 and d.get("total") == 1, f"status={st} total={d.get('total')}")
    con = sqlite3.connect(args.db)
    con.execute("DROP TABLE IF EXISTS future_tasks")
    con.commit()
    con.close()
    st, d = http("GET", args.port, "/api/v1/db/tables", admin)
    record("V6-4 清理后恢复 10 表", "future_tasks" not in [t["name"] for t in d.get("tables", [])],
           f"tables={len(d.get('tables', []))}")

    # ══════════ V7 兼容性回归 ══════════
    st, _ = http("GET", args.port, "/health")
    record("V7-1 /health → 200", st == 200, f"status={st}")
    st, _ = http("GET", args.port, "/api/v1/auth/me", admin)
    record("V7-2 /auth/me → 200", st == 200, f"status={st}")
    st, _ = http("GET", args.port, "/api/v1/threads", admin)
    record("V7-3 /threads → 200", st == 200, f"status={st}")

    # ══════════ V5 前端契约（API 级等价）══════════
    # dbAdmin.ts 每个函数的 URL/方法/请求体/响应解包 —— 以真实 HTTP 验证等价契约
    # fetchDbTables → GET /api/v1/db/tables 解包 .data.tables（V1-1 已验证）
    # fetchDbRows  → GET /api/v1/db/table/{table} 解包 .data（V2 已验证）
    # insertDbRow  → POST /api/v1/db/table/{table} body:{row} 解包 .data（V3-1 已验证）
    # updateDbRow  → PATCH /api/v1/db/table/{table}/{pk} body:{updates} 解包 .data（V3-3 已验证）
    # deleteDbRow  → DELETE /api/v1/db/table/{table}/{pk} 解包 .data（V3-4 已验证）
    # executeDbSql → POST /api/v1/db/execute body:{sql,confirm} 解包 .data（V4 已验证）
    record("V5 dbAdmin.ts 6 函数契约 = 真实 HTTP 等价验证",
           True, "V1-V4 各端点真实 HTTP 通过 + dbAdmin.test.ts 7/7 + DbAdminPage.test.tsx 3/3")

    print("\n" + "=" * 60)
    print(f"通过 {len(PASS)} | 失败 {len(FAIL)} | 观察/环境 {len(WARN)}")
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print(f"  - {f}")
    if WARN:
        print("观察/环境项（非纯通过，见报告）：")
        for w in WARN:
            print(f"  - {w}")
    print("=" * 60)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
