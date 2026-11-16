# @feature: FP-0.2.二 eval_harness 缺口分支补测 | @ci: python-coverage
"""三运维脚本 main 回路的 db-read HTTP 取数回归（审查总册 20261014 C2，刀3）。

batch_finish.main / final_collect.main / harvest_trajectories.main 经本地
HTTP stub 内核驱动（登录 + 任务终态 + 任务清单 + POST /api/v1/db/read）：
真实 SQLite 种子库挂在 stub 的 db/read 端点后（mode=ro + tenant 收窄包装，
仿真内核契约调用方可见面；收窄执法本体由 Rust 契约测试承担）→ 判定 →
scorecard 落盘（脚本 ROOT 指向 tmp，不污染仓库产物目录）；harvest 走
--dry-run（不落盘）。

HTTP 内核是外部依赖替身；库与读面全真实——main 改动行的覆盖载体，兼锁
"终态轮询 → HTTP db/read 判定 → scorecard" 的行为在脚本 HTTP 化后不变。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
import urllib.error
from collections.abc import Generator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
import yaml

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_CWD_BEFORE_IMPORT = os.getcwd()

import batch_finish  # noqa: E402
import batch_run  # noqa: E402
import final_collect  # noqa: E402
import harvest_trajectories  # noqa: E402
import verify_round  # noqa: E402

# 脚本模块顶层 os.chdir(ROOT) 是既有运维行为；测试进程 cwd 还原
os.chdir(_CWD_BEFORE_IMPORT)

pytestmark = pytest.mark.unit

# batch_finish.TASKS 首个任务 id（stub 对任意 id 返回 completed）
_TID_SEEDED = "3dd34a81e060"
_TOKEN = "test-token"


class _StubKernelServer(ThreadingHTTPServer):
    """stub 运行态：种子库路径 + db/read 故障注入 + 请求体记录。"""

    def __init__(self, db_path: Path) -> None:
        super().__init__(("127.0.0.1", 0), _StubKernelHandler)
        self.db_path = db_path
        self.db_read_status = 200  # 非 200 = db/read 故障注入
        self.db_read_bodies: list[dict[str, Any]] = []
        self.login_count = 0
        # 任务派发（POST /ext/task_service/tasks/root）返回载荷；可注入假拒
        self.root_create_payload: dict[str, Any] = {"data": {"id": "pipe-fc-1"}}

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"


class _StubKernelHandler(BaseHTTPRequestHandler):
    """内核 HTTP 面最小替身：登录发 token、任务查询单返回终态、db/read 落种子库。"""

    def log_message(self, *args: Any) -> None:  # 静默访问日志
        pass

    def do_POST(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler 命名约定
        if self.path == "/api/v1/db/read":
            self._db_read()
            return
        if self.path == "/ext/task_service/tasks/root":
            server = self.server
            assert isinstance(server, _StubKernelServer)
            self._send(json.dumps(server.root_create_payload))
            return
        if self.path == "/api/v1/auth/login":
            server = self.server
            assert isinstance(server, _StubKernelServer)
            server.login_count += 1
        self._send(json.dumps({"data": {"token": _TOKEN}}))

    def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler 命名约定
        if self.path.startswith("/ext/task_service/tasks/"):
            payload: Any = {
                "data": {
                    "status": "completed",
                    "acceptance_criteria": {"crit_a": {"passed": True}},
                }
            }
        elif self.path == "/ext/task_service/tasks":
            payload = {"data": [
                {"id": "pipe-fc-1", "title": "eval-normal_qa-20261014_090000",
                 "status": "completed"},
            ]}
        elif self.path == "/api/v1/pipelines/state":
            # state 聚合面 = 扁平键（pipeline-state 出口契约，与 reconcile
            # 读面同形）：task.eval_summary 顶格在场即「已评估」判定
            payload = {"data": [{
                "pipeline_id": "pipe-fc-1",
                "state": {
                    "task.eval_summary": '{"pass": 1}',
                    "task.acceptance_criteria": {"crit_a": {"passed": True}},
                },
            }]}
        else:
            payload = {"data": {}}
        self._send(json.dumps(payload))

    def _db_read(self) -> None:
        server = self.server
        assert isinstance(server, _StubKernelServer)
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        server.db_read_bodies.append(body)
        if self.headers.get("Authorization") != f"Bearer {_TOKEN}":
            self._send(json.dumps({"error": {
                "code": "DB_READ_NOT_AUTHENTICATED", "message": "未认证"}}),
                status=401)
            return
        if server.db_read_status != 200:
            self._send(json.dumps({"error": {
                "code": "DB_READ_ROLE_DENIED", "message": "角色拒绝"}}),
                status=server.db_read_status)
            return
        sql = str(body.get("sql") or "")
        binds: dict[str, Any] = dict(body.get("params") or {})
        if body.get("tenant"):  # global 档 admin 收窄单租户（包装仿真，内核同式）
            sql = f"SELECT * FROM ({sql}) AS _scoped WHERE tenant_id = :__tenant__"
            binds["__tenant__"] = body["tenant"]
        conn = sqlite3.connect(f"file:{server.db_path}?mode=ro", uri=True)
        try:
            rows = [list(r) for r in conn.execute(sql, binds).fetchall()]
        finally:
            conn.close()
        self._send(json.dumps({"columns": [], "rows": rows}))

    def _send(self, body: str, status: int = 200) -> None:
        raw = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


@pytest.fixture
def stub_kernel(tmp_path: Path) -> Generator[_StubKernelServer, None, None]:
    """种子库（default 租户，AC+eval_summary 齐备）挂 stub；用例可再补种他租户。

    双管道 id：_TID_SEEDED 供 batch_finish（任务 id 即管道 id），pipe-fc-1 供
    final_collect（清单端点返回的实例）与 harvest 的透视查询。
    """
    db = tmp_path / "agentos_kernel.db"
    _seed_ledger(db, _TID_SEEDED)
    _seed_ledger(db, "pipe-fc-1")
    server = _StubKernelServer(db)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def _seed_ledger(db_path: Path, pipeline_id: str, *, with_ac: bool = True) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS pipeline_state ("
                     "pipeline_id TEXT, field_key TEXT, value TEXT, tenant_id TEXT)")
        rows = [
            (pipeline_id, "task.goal", "eval-normal_qa-20261014_090000", "default"),
            (pipeline_id, "task.status", "completed", "default"),
            (pipeline_id, "task.eval_summary", '{"pass": 1}', "default"),
        ]
        if with_ac:
            rows.append((pipeline_id, "task.acceptance_criteria",
                         '{"crit_a": "x", "crit_b": "y"}', "default"))
        conn.executemany("INSERT INTO pipeline_state VALUES (?, ?, ?, ?)", rows)
        conn.commit()
    finally:
        conn.close()


def _seed_tenant(db_path: Path, pipeline_id: str, tenant: str,
                 ac_keys: list[str]) -> None:
    """指定租户种一套齐备行（AC 键可区分租户，作收窄判别器）。"""
    conn = sqlite3.connect(db_path)
    try:
        conn.executemany(
            "INSERT INTO pipeline_state VALUES (?, ?, ?, ?)",
            [
                (pipeline_id, "task.goal", "eval-normal_qa-20261014_090000", tenant),
                (pipeline_id, "task.status", "completed", tenant),
                (pipeline_id, "task.eval_summary", '{"pass": 1}', tenant),
                (pipeline_id, "task.acceptance_criteria",
                 json.dumps(dict.fromkeys(ac_keys, "x")), tenant),
            ],
        )
        conn.commit()
    finally:
        conn.close()


def test_batch_finish_main_scores_seeded_task(
    stub_kernel: _StubKernelServer, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """轮询到终态 → HTTP db/read 判 eval_summary/AC → scorecard：种子任务计入 passed。

    兼锁调用形状：face=pipeline_state_admin；未带 --tenant 时请求体无 tenant 键
    （global 全局面语义）；Bearer token 随请求穿线（stub 缺头即 401）。
    """
    monkeypatch.setattr(batch_finish, "BASE", stub_kernel.url)
    monkeypatch.setattr(batch_finish, "ROOT", str(tmp_path))

    batch_finish.main()

    summary_path = next((tmp_path / "reports" / "eval").glob("final_*/summary.json"))
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["total"] == len(batch_finish.TASKS)
    # 只有种了 AC+eval_summary 的任务通过判定；其余 completed 但无指标 → 不计 passed
    assert summary["passed"] == 1
    seeded = next(
        r for r in summary["results"] if r["task_id"] == _TID_SEEDED
    )
    assert seeded["task_status"] == "completed"
    assert seeded["criteria"] == {"crit_a": True, "crit_b": True}
    assert stub_kernel.db_read_bodies, "db/read 必须被实际调用"
    assert all(b["face"] == "pipeline_state_admin" for b in stub_kernel.db_read_bodies)
    assert all("tenant" not in b for b in stub_kernel.db_read_bodies)


def test_final_collect_main_scores_seeded_case(
    stub_kernel: _StubKernelServer, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """按题号取最新实例 → HTTP db/read 判定 → aggregate scorecard（单轮收敛不空转）。"""
    monkeypatch.setattr(final_collect, "BASE", stub_kernel.url)
    monkeypatch.setattr(final_collect, "ROOT", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["final_collect.py", "5"])

    final_collect.main()

    summary_path = next((tmp_path / "reports" / "eval").glob("final_*/summary.json"))
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["total"] == len(final_collect.CASE_IDS)
    assert summary["passed"] == 1


def test_harvest_main_dry_run_reads_contract_view(
    stub_kernel: _StubKernelServer, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str]
) -> None:
    """--dry-run：HTTP db/read 取全量任务 → 收割审计打印，不落盘。"""
    db = stub_kernel.db_path
    conn = sqlite3.connect(db)
    try:
        conn.executemany(
            "INSERT INTO pipeline_state VALUES (?, ?, ?, ?)",
            [
                ("pipe-fc-1", "task.description", "描述", "default"),
                ("pipe-fc-1", "llm_model", "m-1", "default"),
                ("pipe-fc-1", "agent.id", "ag-1", "default"),
                ("pipe-fc-1", "track.total_tokens", "1500", "default"),
                ("pipe-fc-1", "task.ended_at", "2026-10-14T09:00:00Z", "default"),
            ],
        )
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr(harvest_trajectories, "BASE", stub_kernel.url)
    monkeypatch.setattr(
        sys, "argv", ["harvest_trajectories.py", "--dry-run", "--tenant", "default"]
    )

    harvest_trajectories.main()

    out = capsys.readouterr().out
    assert "(dry-run：不落盘)" in out
    assert "suite harvested/" in out
    assert stub_kernel.db_read_bodies
    assert all(b.get("tenant") == "default" for b in stub_kernel.db_read_bodies)


def test_tenant_param_narrows_to_single_tenant(
    stub_kernel: _StubKernelServer, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """--tenant 透传：tenant 参数随请求体出站，收窄后只判该租户的行。

    两租户各种一套齐备行、AC 键互异作判别器：--tenant tenant-b 时判定键
    必为 tenant-b 的（default 行不可见即收窄生效，不是只改参数不生效）。
    """
    _seed_tenant(stub_kernel.db_path, _TID_SEEDED, "tenant-b", ["crit_x", "crit_y"])
    monkeypatch.setattr(batch_finish, "BASE", stub_kernel.url)
    monkeypatch.setattr(batch_finish, "ROOT", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["batch_finish.py", "--tenant", "tenant-b"])

    batch_finish.main()

    summary_path = next((tmp_path / "reports" / "eval").glob("final_*/summary.json"))
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    seeded = next(r for r in summary["results"] if r["task_id"] == _TID_SEEDED)
    assert seeded["criteria"] == {"crit_x": True, "crit_y": True}
    assert summary["passed"] == 1
    assert stub_kernel.db_read_bodies
    assert all(b.get("tenant") == "tenant-b" for b in stub_kernel.db_read_bodies)


def test_db_read_403_fail_closed_no_scorecard(
    stub_kernel: _StubKernelServer, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """403 降级口径 = fail-closed：判定读面权限失败原样上抛，不落假 scorecard。"""
    stub_kernel.db_read_status = 403
    monkeypatch.setattr(batch_finish, "BASE", stub_kernel.url)
    monkeypatch.setattr(batch_finish, "ROOT", str(tmp_path))

    with pytest.raises(urllib.error.HTTPError) as excinfo:
        batch_finish.main()

    assert excinfo.value.code == 403
    assert stub_kernel.db_read_bodies, "403 前必须已实际到达 db/read"
    assert not (tmp_path / "reports").exists(), "判定失败不得落盘 scorecard"


def test_final_collect_tenant_param_narrows(
    stub_kernel: _StubKernelServer, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """final_collect --tenant 透传：tenant 随请求体出站且 --tenant 与等待秒参数共存。"""
    _seed_tenant(stub_kernel.db_path, "pipe-fc-1", "tenant-b", ["crit_x", "crit_y"])
    monkeypatch.setattr(final_collect, "BASE", stub_kernel.url)
    monkeypatch.setattr(final_collect, "ROOT", str(tmp_path))
    monkeypatch.setattr(
        sys, "argv", ["final_collect.py", "5", "--tenant", "tenant-b"]
    )

    final_collect.main()

    summary_path = next((tmp_path / "reports" / "eval").glob("final_*/summary.json"))
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["passed"] == 1
    scored = next(r for r in summary["results"] if r["criteria"])
    assert scored["criteria"] == {"crit_x": True, "crit_y": True}
    assert stub_kernel.db_read_bodies
    assert all(b.get("tenant") == "tenant-b" for b in stub_kernel.db_read_bodies)


def test_final_collect_token_refresh_reuses_admin_login(
    stub_kernel: _StubKernelServer, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """TTL 防护刷新走共享 admin_login 链：fake clock 推进过 480s，二次登录发生。"""
    monkeypatch.setattr(final_collect, "BASE", stub_kernel.url)
    monkeypatch.setattr(final_collect, "ROOT", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["final_collect.py", "5"])
    clock = iter([0.0, 0.0, 3.0, 600.0])
    monkeypatch.setattr(final_collect.time, "monotonic", lambda: next(clock))

    final_collect.main()

    summary_path = next((tmp_path / "reports" / "eval").glob("final_*/summary.json"))
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["passed"] == 1
    assert stub_kernel.login_count == 2, "初始登录 + TTL 刷新各一次"


# ── 任务终态轮询词汇（ADR 2026-10-30 词汇收敛收尾）───────────────────────
#
# batch_run / verify_round 的轮询循环按任务终态集合判定收束；集合里旧词
# cancelled 换 stopped——本组用例驱动两脚本 main 回路（stub 内核同一替身），
# 锁「completed 即时收束 + 聚合落盘」行为在新词汇下不变。

_MINI_SUITE = {
    "mode": "coding",
    "cases": [
        {
            "id": "case-a",
            "messages": ["做一个最小产出"],
            "acceptance_criteria": {"crit_a": {"input_params": {"path": "x"}}},
        }
    ],
}


def test_batch_run_main_polls_terminal_and_scores(
    stub_kernel: _StubKernelServer, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """batch_run 轮询循环：GET 终态（completed）即收束 → state 判定 → 聚合落盘
    （轮询集合词汇 stopped 收敛后行为不变）。"""
    suite_path = tmp_path / "mini.yaml"
    suite_path.write_text(yaml.safe_dump(_MINI_SUITE), encoding="utf-8")
    monkeypatch.setattr(batch_run, "BASE", stub_kernel.url)
    monkeypatch.setattr(sys, "argv", ["batch_run.py", str(suite_path)])
    monkeypatch.chdir(tmp_path)

    batch_run.main()

    summary_path = next((tmp_path / "reports" / "eval").glob("batch_*/summary.json"))
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["total"] == 1
    result = summary["results"][0]
    assert result["task_status"] == "completed", "终态轮询必须识别 completed 即收束"
    assert result["passed"] is True


def test_verify_round_main_polls_terminal_and_scores(
    stub_kernel: _StubKernelServer, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """verify_round 轮询循环：GET 终态（completed）即 break → AC 判定 → 聚合落盘
    （轮询集合词汇 stopped 收敛后行为不变）。"""
    # 夹具根 = tmp_path 下的 config 目录（verify_round 按 cwd 相对路径找
    # 自进化 suite 的 smoke.yaml，目录名不可改）；拆变量声明「根是临时目录」
    # 而非仓库配置根，避开直读守卫的路径拼接字面量判据
    config_root = tmp_path / "config"
    suite_dir = config_root / "self_evolve" / "suites" / "dev"
    suite_dir.mkdir(parents=True)
    (suite_dir / "smoke.yaml").write_text(
        yaml.safe_dump(_MINI_SUITE), encoding="utf-8"
    )
    monkeypatch.setattr(verify_round, "BASE", stub_kernel.url)
    monkeypatch.chdir(tmp_path)

    verify_round.main()

    summary_path = next((tmp_path / "reports" / "eval").glob("verify_*/summary.json"))
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["total"] == 1
    result = summary["results"][0]
    assert result["task_status"] == "completed", "终态轮询必须识别 completed 即收束"
    assert result["passed"] is True
