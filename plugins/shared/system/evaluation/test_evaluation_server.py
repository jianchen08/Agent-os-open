# @feature: 评估服务读面与 stub 执行面 | @vision: V3 可嵌入 | @ci: python-coverage
"""evaluation 插件 server.py 行为测试。

覆盖：
1. evaluation.run：file_check 真实执行（存在/缺失）、未注册类型与
   stub 类型（bash/semantic/human）诚实判失败、汇总计数守恒、
   gate 字段如实报告"未实现"；
2. evaluation.get_result：run→get 往返、未知 id 错误；
3. http.handle：metrics 列表（yaml 读面 + category/status 过滤 + 分页 +
   非法分页参数回退）、单项 404、内置只读 DELETE 405、未知 path 404；
4. 读面底层：_load_metrics 缺文件/坏 yaml 回退空表、_project_root
   环境变量与上溯兜底、_metric_to_response 字段补齐默认值。
"""
from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_DIR = Path(__file__).resolve().parent


def _load_server_module(monkeypatch: pytest.MonkeyPatch, project_root: Path) -> Any:
    """按显式路径加载 server.py（裸名 server 全车道共跑会被劫持）。"""
    mod_name = "evaluation_server_test"
    spec = importlib.util.spec_from_file_location(mod_name, str(_DIR / "server.py"))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    monkeypatch.setenv("AGENTOS_PROJECT_ROOT", str(project_root))
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def metrics_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """带汇总 yaml 的项目根。"""
    cfg = tmp_path / "config" / "evaluation"
    cfg.mkdir(parents=True)
    (cfg / "evaluation_metrics.yaml").write_text(
        """
metrics:
  - name: m_file
    description: 文件检查
    category: functional
    evaluator_type: builtin
    level: 2
    tags: [t1]
  - name: m_bash
    description: 命令检查
    category: functional
    status: deprecated
    is_red_line: true
  - name: m_sem
    description: 语义检查
    category: quality
""",
        encoding="utf-8",
    )
    return tmp_path


class TestEvaluationRun:
    def test_file_check_pass_and_fail(self, metrics_root: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        srv = _load_server_module(monkeypatch, metrics_root)
        target = tmp_path / "exists.txt"
        target.write_text("x", encoding="utf-8")
        summary = asyncio.run(
            srv.evaluation_run(
                task_id="t1",
                metrics=[
                    {"metric_id": "m_file", "type": "file_check", "params": {"path": str(target)}},
                    {"metric_id": "m_miss", "type": "file_check", "params": {"path": str(tmp_path / "nope.txt")}},
                ],
            )
        )
        assert summary["total"] == 2
        assert summary["passed"] == 1 and summary["failed"] == 1
        assert summary["all_passed"] is False
        # 计数守恒（性质断言）
        assert summary["passed"] + summary["failed"] == summary["total"]
        by_id = {r["metric_id"]: r for r in summary["results"]}
        assert by_id["m_file"]["passed"] is True
        assert "file exists" in by_id["m_file"]["message"]
        assert by_id["m_miss"]["passed"] is False

    def test_unknown_and_stub_types_fail_honestly(self, metrics_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        srv = _load_server_module(monkeypatch, metrics_root)
        summary = asyncio.run(
            srv.evaluation_run(
                task_id="t2",
                metrics=[
                    {"metric_id": "x", "type": "no_such", "params": {}},
                    {"metric_id": "b", "type": "bash_check", "params": {"command": "ls"}},
                    {"metric_id": "h", "type": "human_review", "params": {}},
                ],
            )
        )
        assert summary["all_passed"] is False
        errs = {r["metric_id"]: r.get("error", "") for r in summary["results"]}
        assert "unknown metric type" in errs["x"]
        assert "not implemented" in errs["b"]
        assert "not implemented" in errs["h"]

    def test_gate_fields_report_unimplemented(self, metrics_root: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        srv = _load_server_module(monkeypatch, metrics_root)
        f = tmp_path / "ok.txt"
        f.write_text("", encoding="utf-8")
        summary = asyncio.run(
            srv.evaluation_run(
                task_id="t3",
                gate_mode=True,
                metrics=[{"metric_id": "m", "type": "file_check", "params": {"path": str(f)}}],
            )
        )
        assert summary["all_passed"] is True
        assert summary["gate_mode"] is True
        # 诚实语义：gate 未接线，不得伪装已拦截
        assert summary["gated"] is False
        assert summary["gate_enforced"] is False
        assert summary["gate_enforced"] is False

    def test_get_result_roundtrip_and_missing(self, metrics_root: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        srv = _load_server_module(monkeypatch, metrics_root)
        f = tmp_path / "a.txt"
        f.write_text("", encoding="utf-8")
        summary = asyncio.run(
            srv.evaluation_run(
                task_id="t4",
                metrics=[{"metric_id": "m", "type": "file_check", "params": {"path": str(f)}}],
            )
        )
        got = asyncio.run(srv.get_result(summary["eval_id"]))
        assert got["eval_id"] == summary["eval_id"]
        missing = asyncio.run(srv.get_result("eval_nonexistent"))
        assert missing["error"] == "evaluation not found"


def _decode_body(envelope: dict[str, Any]) -> tuple[int, Any]:
    assert envelope["success"] is True
    resp = envelope["data"]
    body = json.loads(base64.b64decode(resp["body"]).decode("utf-8"))
    return resp["status"], body


class TestHttpHandleMetricsList:
    def test_list_all(self, metrics_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        srv = _load_server_module(monkeypatch, metrics_root)
        status, body = _decode_body(
            asyncio.run(
                srv.http_handle(path="/ext/evaluation_service/metrics", method="GET")
            )
        )
        assert status == 200
        assert body["total"] == 3
        assert {m["id"] for m in body["metrics"]} == {"m_file", "m_bash", "m_sem"}

    def test_filter_by_category_and_status(self, metrics_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        srv = _load_server_module(monkeypatch, metrics_root)
        _, body = _decode_body(
            asyncio.run(
                srv.http_handle(
                    path="/ext/evaluation_service/metrics",
                    method="GET",
                    query={"category": "functional"},
                )
            )
        )
        assert {m["id"] for m in body["metrics"]} == {"m_file", "m_bash"}
        assert body["total"] == 2

        _, body2 = _decode_body(
            asyncio.run(
                srv.http_handle(
                    path="/ext/evaluation_service/metrics",
                    method="GET",
                    query={"status": "deprecated"},
                )
            )
        )
        assert [m["id"] for m in body2["metrics"]] == ["m_bash"]

    def test_filter_by_metric_type(self, metrics_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """metric_type 过滤与任意过滤组合（前端的可选查询参数）。"""
        srv = _load_server_module(monkeypatch, metrics_root)
        _, body = _decode_body(
            asyncio.run(
                srv.http_handle(
                    path="/ext/evaluation_service/metrics",
                    method="GET",
                    query={"metric_type": "builtin"},
                )
            )
        )
        # yaml 中仅有 m_file 声明 evaluator_type=builtin；未命中则空
        assert {m["id"] for m in body["metrics"]} == {"m_file"}

        _, none = _decode_body(
            asyncio.run(
                srv.http_handle(
                    path="/ext/evaluation_service/metrics",
                    method="GET",
                    query={"metric_type": "no-such-type"},
                )
            )
        )
        assert none["metrics"] == [] and none["total"] == 0

    def test_pagination_zero_limit_and_bad_limit(self, metrics_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """limit=0 → 回退全量（0 表示未指定，等价不传）。"""
        srv = _load_server_module(monkeypatch, metrics_root)
        _, page = _decode_body(
            asyncio.run(
                srv.http_handle(
                    path="/ext/evaluation_service/metrics",
                    method="GET",
                    query={"skip": "0", "limit": "0"},
                )
            )
        )
        assert len(page["metrics"]) == 3 and page["total"] == 3

    def test_pagination_and_invalid_params_fallback(self, metrics_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        srv = _load_server_module(monkeypatch, metrics_root)
        _, page = _decode_body(
            asyncio.run(
                srv.http_handle(
                    path="/ext/evaluation_service/metrics",
                    method="GET",
                    query={"skip": "1", "limit": "1"},
                )
            )
        )
        assert page["total"] == 3
        assert len(page["metrics"]) == 1

        # 非法分页参数 → 回退全量
        _, full = _decode_body(
            asyncio.run(
                srv.http_handle(
                    path="/ext/evaluation_service/metrics",
                    method="GET",
                    query={"skip": "abc"},
                )
            )
        )
        assert len(full["metrics"]) == 3


class TestHttpHandleSingleAndErrors:
    def test_single_metric_found(self, metrics_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        srv = _load_server_module(monkeypatch, metrics_root)
        env = asyncio.run(
            srv.http_handle(path="/ext/evaluation_service/metrics/m_bash", method="GET")
        )
        status, body = _decode_body(env)
        assert status == 200
        assert body["id"] == "m_bash"
        assert body["is_red_line"] is True

    def test_single_metric_404(self, metrics_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        srv = _load_server_module(monkeypatch, metrics_root)
        env = asyncio.run(
            srv.http_handle(path="/ext/evaluation_service/metrics/ghost", method="GET")
        )
        assert env["success"] is False
        assert env["data"]["status"] == 404

    def test_delete_rejected_readonly(self, metrics_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        srv = _load_server_module(monkeypatch, metrics_root)
        env = asyncio.run(
            srv.http_handle(path="/ext/evaluation_service/metrics/m_file", method="DELETE")
        )
        assert env["success"] is False
        assert env["data"]["status"] == 405

    def test_unknown_path_404_envelope(self, metrics_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        srv = _load_server_module(monkeypatch, metrics_root)
        status, body = _decode_body(
            asyncio.run(srv.http_handle(path="/ext/evaluation_service/nope", method="GET"))
        )
        assert status == 404
        assert body["error"] == "not found"

    def test_on_load_and_registry_resource(self, metrics_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """on_load 钩子 + 指标注册表资源暴露（manifest 声明面）。"""
        srv = _load_server_module(monkeypatch, metrics_root)
        assert asyncio.run(srv._on_load({})) is None
        reg = srv._metric_registry_resource()
        assert {"file_check", "bash_check", "semantic_check", "human_review"} == set(reg["metrics"])


class TestReadFaceHelpers:
    def test_load_metrics_missing_file_and_bad_yaml(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # 无 yaml 的根 → 空表不抛
        srv = _load_server_module(monkeypatch, tmp_path)
        assert srv._load_metrics() == []

        bad = tmp_path / "config" / "evaluation"
        bad.mkdir(parents=True)
        (bad / "evaluation_metrics.yaml").write_text("metrics: [ {name: ,", encoding="utf-8")
        assert srv._load_metrics() == []

    def test_project_root_env_wins_and_upward_fallback(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        root = tmp_path / "proj"
        (root / "config").mkdir(parents=True)
        srv = _load_server_module(monkeypatch, tmp_path)
        monkeypatch.setenv("AGENTOS_PROJECT_ROOT", str(root))  # 加载后再指向 root
        assert srv._project_root() == str(root)

        # env 指向不存在目录 → 上溯找 config/ 兜底（当前工作目录即含 config/）
        monkeypatch.setenv("AGENTOS_PROJECT_ROOT", str(tmp_path / "nope"))
        import os

        assert srv._project_root() == os.getcwd()

    def test_project_root_upward_walk_and_unreachable(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """env 未设且 cwd 不可达 → 上溯 6 层仍无 config/ → 返回 cwd。"""
        import os

        srv = _load_server_module(monkeypatch, tmp_path)
        monkeypatch.delenv("AGENTOS_PROJECT_ROOT", raising=False)
        prev = os.getcwd()
        try:
            os.chdir(tmp_path)  # 临时目录无 config/ 且上溯 6 层到盘根仍无
            assert srv._project_root() == os.getcwd()
        finally:
            os.chdir(prev)
    def test_metric_to_response_defaults(self, metrics_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        srv = _load_server_module(monkeypatch, metrics_root)
        got = srv._metric_to_response({"name": "n1"})
        assert got["id"] == "n1"
        assert got["level"] == 0
        assert got["default_weight"] == 1.0
        assert got["is_red_line"] is False
        assert got["source"] == "builtin"
        assert got["status"] == "active"
        assert got["includes"] == [] and got["requires"] == []
        assert got["usage_count"] == 0
        assert got["avg_execution_time"] is None

        got2 = srv._metric_to_response(
            {"id": "alt", "category": "c", "level": 3, "default_weight": 0.5, "tags": ["x"]}
        )
        assert got2["id"] == "alt" and got2["name"] == ""
        assert got2["level"] == 3 and got2["default_weight"] == 0.5
