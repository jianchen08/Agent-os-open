# @feature: FP-0.2.六 记忆检索 | @vision: V1 可进化 | @ci: python-coverage
"""server.py 剩余缺口分支补充覆盖（行号锚定 2026-09-13 插桩车道）。

主用例见 test_hindsight_server.py / test_hindsight_server_http.py /
test_hindsight_server_lifecycle.py / test_hindsight_kb_routes_extra.py；本文件补：

1. ``_resolve_unit_document_id``：client 无 memory 面 / get_memory 不可调用 /
   model_dump 形态 / __dict__ 形态 / 全不可解析 → None；
2. ``_resolve_env_ref``：非引用式原样返回；
3. ``_apply_llm_env``：api_base 模型条目与 provider 条目均为空 → 警告且
   BASE_URL 不注入（端点/模型/key 照注入）；
4. ``_spawn_stderr_drain``：stderr 读流异常 → 排空线程自终止留痕；
5. ``_start_api_server``：venv python 的 Unix 布局回退（bin/python）；
6. ``_wait_api_ready``：子进程已退出且 stderr tail 读失败 → 空 tail 抛错；
7. ``_on_load``：acreate_bank 失败（bank 已存在等）→ debug 留痕不崩；
8. knowledge-base HTTP 面：/search top_k 非整数回落默认、/upload multipart
   解析失败 400、未匹配路由 404。

mock 仅限外部依赖（hindsight client / subprocess / 文件系统错误注入）。
"""

from __future__ import annotations

import base64
import importlib.util
import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

_SDK_SRC = Path(__file__).resolve().parents[4] / "sdk" / "src"
if _SDK_SRC.exists() and str(_SDK_SRC) not in sys.path:
    sys.path.insert(0, str(_SDK_SRC))

_HINDSIGHT_ENV_KEYS = (
    "HINDSIGHT_API_LLM_PROVIDER",
    "HINDSIGHT_API_LLM_BASE_URL",
    "HINDSIGHT_API_LLM_MODEL",
    "HINDSIGHT_API_LLM_API_KEY",
    "HINDSIGHT_API_RERANKER_PROVIDER",
)


def _load_module() -> Any:
    """动态加载 server.py（每用例新建，模块级状态不跨测试污染）。"""
    mod_name = "hindsight_memory_server_gaps_test"
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "server.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def _run(coro: Any) -> Any:
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture
def srv() -> Any:
    return _load_module()


@pytest.fixture(autouse=True)
def _clear_hindsight_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in _HINDSIGHT_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


# ═══════════════════════════════════════════════════════════
# _resolve_unit_document_id（删除回退的文档 id 解析）
# ═══════════════════════════════════════════════════════════


class TestResolveUnitDocumentId:
    def test_client_without_memory_face_returns_none(self, srv: Any) -> None:
        srv._client = SimpleNamespace()
        assert _run(srv._resolve_unit_document_id("b1", "m1")) is None

    def test_get_memory_not_callable_returns_none(self, srv: Any) -> None:
        srv._client = SimpleNamespace(memory=SimpleNamespace(get_memory="not-callable"))
        assert _run(srv._resolve_unit_document_id("b1", "m1")) is None

    def test_pydantic_style_unit_uses_model_dump(self, srv: Any) -> None:
        unit = SimpleNamespace(model_dump=lambda: {"document_id": "doc-1"})
        client = SimpleNamespace(memory=SimpleNamespace(get_memory=AsyncMock(return_value=unit)))
        srv._client = client
        assert _run(srv._resolve_unit_document_id("b1", "m1")) == "doc-1"

    def test_plain_object_unit_uses_vars(self, srv: Any) -> None:
        class _Unit:
            def __init__(self) -> None:
                self.document_id = "doc-2"

        client = SimpleNamespace(memory=SimpleNamespace(get_memory=AsyncMock(return_value=_Unit())))
        srv._client = client
        assert _run(srv._resolve_unit_document_id("b1", "m1")) == "doc-2"

    def test_unresolvable_unit_and_empty_doc_id_return_none(self, srv: Any) -> None:
        bare = SimpleNamespace(memory=SimpleNamespace(get_memory=AsyncMock(return_value=object())))
        srv._client = bare
        assert _run(srv._resolve_unit_document_id("b1", "m1")) is None

        empty = SimpleNamespace(
            memory=SimpleNamespace(get_memory=AsyncMock(return_value={"document_id": ""}))
        )
        srv._client = empty
        assert _run(srv._resolve_unit_document_id("b1", "m1")) is None


# ═══════════════════════════════════════════════════════════
# _resolve_env_ref（非引用式）
# ═══════════════════════════════════════════════════════════


class TestResolveEnvRef:
    def test_plain_value_returned_as_is(self, srv: Any) -> None:
        assert srv._resolve_env_ref("sk-raw-key", {"ANY": "x"}) == "sk-raw-key"

    def test_reference_resolves_from_process_env_first(self, srv: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MY_SECRET_VAR", "from-env")
        assert srv._resolve_env_ref("${MY_SECRET_VAR}", {"MY_SECRET_VAR": "from-file"}) == "from-env"
        assert srv._resolve_env_ref("${OTHER_VAR}", {"OTHER_VAR": "from-file"}) == "from-file"
        assert srv._resolve_env_ref("${MISSING_VAR}", {}) == ""


# ═══════════════════════════════════════════════════════════
# _apply_llm_env：api_base 全空段
# ═══════════════════════════════════════════════════════════


_LLM_YAML_NO_BASE = """\
defaults:
  chat: m-chat
models:
  m-chat:
    model_name: ChatM
    provider: pchat
providers:
  pchat:
    type: openai
    keys:
      - api_key: "ck"
"""


class TestApplyLlmEnvEmptyApiBase:
    def test_empty_api_base_warns_but_injects_rest(
        self, srv: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """api_base 在模型条目与 provider 条目均为空：警告留痕，BASE_URL 不注入。"""
        root = tmp_path / "a" / "b" / "c" / "hindsight_memory"
        root.mkdir(parents=True)
        (tmp_path / "config" / "models").mkdir(parents=True)
        (tmp_path / "config" / "models" / "llm.yaml").write_text(_LLM_YAML_NO_BASE, encoding="utf-8")
        monkeypatch.setattr(srv, "_THIS_DIR", str(root))

        with caplog.at_level("WARNING"):
            srv._apply_llm_env()

        assert "HINDSIGHT_API_LLM_BASE_URL" not in os.environ
        assert os.environ["HINDSIGHT_API_LLM_MODEL"] == "ChatM"
        assert os.environ["HINDSIGHT_API_LLM_API_KEY"] == "ck"
        assert os.environ["HINDSIGHT_API_LLM_PROVIDER"] == "openai"
        assert any(
            "api_base" in r.getMessage() and "m-chat" in r.getMessage() for r in caplog.records
        )


# ═══════════════════════════════════════════════════════════
# _spawn_stderr_drain / _start_api_server / _wait_api_ready
# ═══════════════════════════════════════════════════════════


def _drain_thread_alive() -> bool:
    return any(t.name == "hindsight-stderr-drain" and t.is_alive() for t in threading.enumerate())


class TestStderrDrainAndServer:
    def test_drain_thread_survives_read_error(self, srv: Any, tmp_path: Path) -> None:
        """stderr 流读异常：排空线程自终止并留痕，不拖垮父进程。"""
        bad_stream = MagicMock()
        bad_stream.readline.side_effect = OSError("pipe broken")
        log_path = tmp_path / "stderr.log"

        records: list[logging.LogRecord] = []

        class _Cap(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        cap = _Cap()
        drain_logger = logging.getLogger("hindsight_api.stderr")
        drain_logger.addHandler(cap)
        try:
            srv._spawn_stderr_drain(SimpleNamespace(stderr=bad_stream), str(log_path))
            deadline = time.monotonic() + 5
            while _drain_thread_alive() and time.monotonic() < deadline:
                time.sleep(0.05)
        finally:
            drain_logger.removeHandler(cap)

        assert not _drain_thread_alive()
        assert any("stderr 排空异常终止" in r.getMessage() for r in records)

    def test_drain_thread_survives_handler_build_failure(self, srv: Any, tmp_path: Path) -> None:
        """落盘日志所在目录不可建（父目录缺失）：自终止留痕，不抛未处理异常。"""
        bad_log_path = tmp_path / "no_such_dir" / "nested" / "stderr.log"

        records: list[logging.LogRecord] = []

        class _Cap(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        cap = _Cap()
        drain_logger = logging.getLogger("hindsight_api.stderr")
        drain_logger.addHandler(cap)
        try:
            srv._spawn_stderr_drain(SimpleNamespace(stderr=None), str(bad_log_path))
            deadline = time.monotonic() + 5
            while _drain_thread_alive() and time.monotonic() < deadline:
                time.sleep(0.05)
        finally:
            drain_logger.removeHandler(cap)

        assert not _drain_thread_alive()
        assert any("stderr 排空异常终止" in r.getMessage() for r in records)

    def test_unix_layout_venv_python_fallback(self, srv: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Windows Scripts/python.exe 缺失时回退 Unix bin/python 布局。"""
        root = tmp_path / "plugin_root"
        venv_bin = root / ".venv-hindsight" / "bin"
        venv_bin.mkdir(parents=True)
        (venv_bin / "python").write_text("", encoding="utf-8")
        monkeypatch.setattr(srv, "_THIS_DIR", str(root))

        recorded: dict[str, Any] = {}

        def _fake_popen(cmd: list[str], **kwargs: Any) -> Any:
            recorded["cmd"] = cmd
            return SimpleNamespace(pid=4321, stderr=None)

        monkeypatch.setattr(subprocess, "Popen", _fake_popen)
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        process, stderr_path = srv._start_api_server(18431, str(data_dir))

        assert process.pid == 4321
        assert recorded["cmd"][0].replace("\\", "/").endswith(".venv-hindsight/bin/python")
        assert stderr_path.endswith("hindsight_api_stderr.log")

    def test_wait_ready_reports_exit_with_unreadable_tail(
        self, srv: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """子进程已退出且 stderr tail 落盘不可读：空 tail 进错误信息，不二次崩。"""
        import urllib.request

        def _raise(*args: Any, **kwargs: Any) -> Any:
            raise OSError("connection refused")

        monkeypatch.setattr(urllib.request, "urlopen", _raise)
        exited = SimpleNamespace(poll=lambda: 1, returncode=-9)
        missing_log = str(tmp_path / "no_dir" / "stderr.log")

        with pytest.raises(RuntimeError, match="子进程已退出"):
            _run(srv._wait_api_ready("http://127.0.0.1:18432", exited, missing_log))


# ═══════════════════════════════════════════════════════════
# _on_load：acreate_bank 失败容错
# ═══════════════════════════════════════════════════════════


class TestOnLoadBankCreation:
    def test_acreate_bank_failure_degrades_to_debug_log(
        self, srv: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """默认 bank 已存在等创建失败：debug 留痕，on_load 正常完成。"""
        root = tmp_path / "a" / "b" / "c" / "hindsight_memory"
        root.mkdir(parents=True)
        monkeypatch.setattr(srv, "_THIS_DIR", str(root))
        monkeypatch.setattr(srv.plugin, "get_config", lambda: {"data_dir": str(tmp_path / "data")})
        monkeypatch.setattr(srv, "_hindsight_api_up", lambda base_url: True)

        fake_client = MagicMock()
        fake_client.acreate_bank = AsyncMock(side_effect=RuntimeError("bank already exists"))
        fake_module = SimpleNamespace(Hindsight=lambda base_url: fake_client)
        monkeypatch.setitem(sys.modules, "hindsight_client", fake_module)

        # spec 加载模块的 logger（__name__ = 测试模块名）级默认 NOTSET，
        # 生效级继承 root（WARNING）——DEBUG 记录须整体放宽 root 才能被 caplog 捕获
        with caplog.at_level(logging.DEBUG):
            _run(srv._on_load({}))

        assert srv._client is fake_client
        fake_client.acreate_bank.assert_awaited_once()
        assert any("创建默认 bank" in r.getMessage() for r in caplog.records)


# ═══════════════════════════════════════════════════════════
# knowledge-base HTTP 面
# ═══════════════════════════════════════════════════════════


def _load_kb(tmp_path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(
        "hindsight_kb_gaps_test", _PLUGIN_DIR / "knowledge_base.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["hindsight_kb_gaps_test"] = module
    spec.loader.exec_module(module)
    module.set_data_dir(str(tmp_path / "kb"))
    return module


_KB_PREFIX = "/ext/hindsight_memory_service/knowledge-base"


class TestKbHttpGaps:
    def test_search_non_integer_top_k_falls_back_to_default(
        self, srv: Any, tmp_path: Path
    ) -> None:
        """GET /search?top_k=abc：非整数回落默认 10，检索照常执行。"""
        kb = _load_kb(tmp_path)
        client = MagicMock()
        client.arecall = AsyncMock(return_value={"results": []})
        kb.set_client(client)
        srv._client = client

        resp = _run(srv._handle_kb_domain(_KB_PREFIX + "/search", "GET", "", {"query": "x", "top_k": "abc"}, {}))
        assert resp["success"] is True
        payload = json.loads(base64.b64decode(resp["data"]["body"]).decode("utf-8"))
        assert payload["total"] == 0
        assert client.arecall.await_args.kwargs["query"] == "x"

    def test_upload_multipart_parse_failure_is_400(
        self, srv: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """POST /upload multipart 解析抛错（共享层 http_json 边界故障注入）→ 400。"""
        kb = _load_kb(tmp_path)
        kb.set_client(None)
        srv._client = None

        def _boom(content_type: str, body: bytes) -> dict[str, Any]:
            raise ValueError("malformed multipart body")

        monkeypatch.setattr(srv, "_parse_multipart", _boom)
        body = base64.b64encode(b"--xyz\r\nbroken").decode("ascii")
        resp = _run(srv._handle_kb_domain(
            _KB_PREFIX + "/upload", "POST", body,
            {}, {"content-type": "multipart/form-data; boundary=xyz"},
        ))
        assert resp["success"] is True
        data = resp["data"]
        assert data["status"] == 400
        payload = json.loads(base64.b64decode(data["body"]).decode("utf-8"))
        assert "multipart parse failed" in payload["error"]

    def test_unmatched_route_returns_404(self, srv: Any, tmp_path: Path) -> None:
        """已知前缀下未匹配的 sub+method 组合 → 404 not found。"""
        kb = _load_kb(tmp_path)
        kb.set_client(None)
        srv._client = None

        resp = _run(srv._handle_kb_domain(_KB_PREFIX + "/stats", "POST", "", {}, {}))
        assert resp["success"] is True
        data = resp["data"]
        assert data["status"] == 404
        payload = json.loads(base64.b64decode(data["body"]).decode("utf-8"))
        assert payload["error"] == "not found"
