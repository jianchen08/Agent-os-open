# @feature: FP-0.2.〇 管道引擎与插件执行模型 | @vision: V3 可嵌入 | @ci: python-coverage
"""evaluation 插件 server.py 行为测试。

覆盖：
1. evaluation.run：file_check 真实执行（存在/缺失）、未注册类型与
   stub 类型（bash/semantic/human）诚实判失败、汇总计数守恒、
   gate 字段如实报告"未实现"；
2. evaluation.get_result：run→get 往返、未知 id 错误；
3. http.handle：metrics 列表（yaml 读面 + category/status 过滤 + 分页 +
   非法分页参数回退）、单项 404、内置只读 DELETE 405、未知 path 404；
4. 读面底层：_load_metrics 缺文件/坏 yaml 上抛、空内容=真空注册表、
   读失败经 http.handle 转 500 错误信封（配置损坏 ≠ 无指标）、
   _project_root 环境变量与上溯兜底、_metric_to_response 字段补齐默认值；
5. 指标注册表 yaml 单源（_load_metric_registry）：键面/params_schema/required
   随定义走，损坏 → 空表诚实判未注册，与生产 yaml 对齐回归锚。
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
import yaml

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


@pytest.fixture
def metrics_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """带汇总 yaml 的项目根。

    内置四指标带 input_schema（与生产 yaml 同构子集）：指标注册表已改为
    yaml 单源加载，注册/未知类型判定与 params_schema 均随定义走。
    """
    cfg = tmp_path / "config" / "plugins" / "evaluation"
    cfg.mkdir(parents=True)
    (cfg / "evaluation_metrics.yaml").write_text(
        """
metrics:
  - name: file_check
    description: 文件检查
    category: file
    evaluator_type: tool
    level: 1
    input_schema:
      type: object
      properties:
        path:
          type: string
          description: 文件或目录路径
      required: [path]
  - name: bash_check
    description: 命令检查
    category: command
    evaluator_type: tool
    input_schema:
      type: object
      properties:
        command:
          type: string
      required: [command]
  - name: semantic_check
    description: 语义检查
    category: semantic
    evaluator_type: agent
    input_schema:
      type: object
      properties:
        output:
          type: string
          description: 要检查的输出
        expected:
          type: string
          description: 期望的语义描述
      required: [output]
  - name: human_review
    description: 人工审核
    category: human
    evaluator_type: human
    input_schema:
      type: object
      properties:
        mode:
          type: string
        title:
          type: string
      required: [mode, title]
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

    def test_results_bounded_storage_stamps_ts_and_read_face_unchanged(
        self, metrics_root: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """D2 接入：_results 经 BoundedDict 收敛——写入条目带 ts，get 读取面不变。"""
        from bounded_dict import BoundedDict

        srv = _load_server_module(monkeypatch, metrics_root)
        assert isinstance(srv._results, BoundedDict)
        f = tmp_path / "b.txt"
        f.write_text("", encoding="utf-8")
        summary = asyncio.run(
            srv.evaluation_run(
                task_id="t-bd",
                metrics=[{"metric_id": "m", "type": "file_check", "params": {"path": str(f)}}],
            )
        )
        stored = srv._results[summary["eval_id"]]
        assert isinstance(stored["ts"], float) and stored["ts"] > 0
        # 读取面零变化：get_result 返回完整 summary（含原有字段）
        got = asyncio.run(srv.get_result(summary["eval_id"]))
        assert got["task_id"] == "t-bd"
        assert got["all_passed"] is True
        assert got["results"] == summary["results"]


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
        assert body["total"] == 7
        assert {m["id"] for m in body["metrics"]} == {
            "file_check",
            "bash_check",
            "semantic_check",
            "human_review",
            "m_file",
            "m_bash",
            "m_sem",
        }

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
        assert len(page["metrics"]) == 7 and page["total"] == 7

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
        assert page["total"] == 7
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
        assert len(full["metrics"]) == 7


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

    def test_metric_registry_resource(self, metrics_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """指标注册表资源 = yaml 单源：键面与 params_schema/required 随定义走。"""
        srv = _load_server_module(monkeypatch, metrics_root)
        reg = srv._metric_registry_resource()
        assert {
            "file_check",
            "bash_check",
            "semantic_check",
            "human_review",
            "m_file",
            "m_bash",
            "m_sem",
        } == set(reg["metrics"])
        # 注册项与 input_schema 同源（params_schema=properties，required 直取）
        assert reg["metrics"]["semantic_check"]["params_schema"] == {
            "output": {"type": "string", "description": "要检查的输出"},
            "expected": {"type": "string", "description": "期望的语义描述"},
        }
        assert reg["metrics"]["semantic_check"]["required"] == ["output"]
        assert reg["metrics"]["human_review"]["required"] == ["mode", "title"]
        # 无 input_schema 的定义 → 空 schema 面（不猜）
        assert reg["metrics"]["m_sem"]["params_schema"] == {}
        assert reg["metrics"]["m_sem"]["required"] == []


class TestMetricRegistrySingleSource:
    """指标注册表 = 汇总 yaml 单源（semantic_check 口径统一回归锚）。"""

    def test_registry_matches_production_yaml(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """生产 yaml（仓库根）驱动注册表：semantic_check required=[expected]
        （白话 expect 映射的 canonical 用法，ADR 2026-09-29-acceptance-flat-fields），
        params_schema 含 output/expected 且无 criteria（旧硬编码 {"criteria"}
        与定义矛盾的双源已消灭）。"""
        repo_root = _DIR.parents[3]
        srv = _load_server_module(monkeypatch, repo_root)
        reg = srv._load_metric_registry()
        assert "semantic_check" in reg
        sem = reg["semantic_check"]
        assert sem["required"] == ["expected"]
        assert "output" in sem["params_schema"]
        assert "expected" in sem["params_schema"]
        assert "criteria" not in sem["params_schema"]

    def test_registry_read_failure_yields_empty(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """yaml 损坏 → 注册表空表（与 http 读面的 5xx 错误信封分立）。"""
        broken = tmp_path / "config" / "plugins" / "evaluation"
        broken.mkdir(parents=True)
        (broken / "evaluation_metrics.yaml").write_text("metrics: [ {name: ,", encoding="utf-8")
        srv = _load_server_module(monkeypatch, tmp_path)
        assert srv._load_metric_registry() == {}

    def test_registry_skips_nameless_entries(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """定义缺 name 的条目跳过（不产生无名注册项）。"""
        cfg = tmp_path / "config" / "plugins" / "evaluation"
        cfg.mkdir(parents=True)
        (cfg / "evaluation_metrics.yaml").write_text(
            "metrics:\n  - description: 无名条目\n  - name: ok_metric\n",
            encoding="utf-8",
        )
        srv = _load_server_module(monkeypatch, tmp_path)
        assert set(srv._load_metric_registry().keys()) == {"ok_metric"}

    def test_unknown_type_when_registry_empty(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """注册表空（yaml 损坏）→ evaluation_run 对任意类型诚实判失败。"""
        broken = tmp_path / "config" / "plugins" / "evaluation"
        broken.mkdir(parents=True)
        (broken / "evaluation_metrics.yaml").write_text("metrics: [ {name: ,", encoding="utf-8")
        srv = _load_server_module(monkeypatch, tmp_path)
        summary = asyncio.run(
            srv.evaluation_run(
                task_id="t-broken",
                metrics=[{"metric_id": "f", "type": "file_check", "params": {}}],
            )
        )
        assert summary["all_passed"] is False
        assert "unknown metric type" in summary["results"][0]["error"]


class TestHttpHandleMetricsReadFailure:
    """指标 yaml 读失败 → 500 错误信封；真空注册表仍 200（两者必须可区分）。"""

    def test_list_endpoint_config_broken_error_envelope(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        srv = _load_server_module(monkeypatch, tmp_path)
        env = asyncio.run(
            srv.http_handle(path="/ext/evaluation_service/metrics", method="GET")
        )
        assert env["success"] is False, "配置损坏不得伪装成空指标列表"
        assert env["data"]["status"] == 500
        assert "评估指标配置读取失败" in env["error"]

    def test_single_endpoint_config_broken_error_envelope(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        srv = _load_server_module(monkeypatch, tmp_path)
        env = asyncio.run(
            srv.http_handle(path="/ext/evaluation_service/metrics/m_x", method="GET")
        )
        assert env["success"] is False
        assert env["data"]["status"] == 500

    def test_list_endpoint_real_empty_registry_still_200(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = tmp_path / "config" / "plugins" / "evaluation"
        cfg.mkdir(parents=True)
        (cfg / "evaluation_metrics.yaml").write_text("metrics: []\n", encoding="utf-8")
        srv = _load_server_module(monkeypatch, tmp_path)
        _, body = _decode_body(
            asyncio.run(
                srv.http_handle(path="/ext/evaluation_service/metrics", method="GET")
            )
        )
        assert body == {"metrics": [], "total": 0}


class TestReadFaceHelpers:
    def test_load_metrics_missing_or_bad_yaml_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """缺文件/坏 yaml → 上抛（配置损坏 ≠ 无指标）。"""
        srv = _load_server_module(monkeypatch, tmp_path)
        with pytest.raises(OSError):
            srv._load_metrics()

        bad = tmp_path / "config" / "plugins" / "evaluation"
        bad.mkdir(parents=True)
        (bad / "evaluation_metrics.yaml").write_text("metrics: [ {name: ,", encoding="utf-8")
        with pytest.raises(yaml.YAMLError):
            srv._load_metrics()

    def test_load_metrics_empty_content_is_real_empty(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """解析成功但注册表为空（空 metrics/空文件）→ 返回 []，与损坏可区分。"""
        cfg = tmp_path / "config" / "plugins" / "evaluation"
        cfg.mkdir(parents=True)
        srv = _load_server_module(monkeypatch, tmp_path)
        (cfg / "evaluation_metrics.yaml").write_text("metrics: []\n", encoding="utf-8")
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
