# @feature: FP-T07 llm api | @ci: python-coverage
"""llm_core _diagnostics / _payload_diag 纯函数与降级路径补测。

契约：payload 诊断默认关闭（AGENTOS_PAYLOAD_DIAG != "1" 不落盘）；开启时写
系统 tempfile 且敏感字段脱敏；写盘失败降级返回 None 不阻断。prompt 审计默认
关闭（AGENTOS_LOG_PROMPT_BODY），开启时经基础脱敏写独立文件。

覆盖分支：
- ``_payload_diag``：redact_payload 递归脱敏、_safe_filename_segment 安全化、
  dump_payload_diag 关闭返回 None / 开启写 tempfile / 写盘失败返回 None；
- ``_diagnostics``：_redact_prompt 三种掩码、_resolve_prompt_log_path 向上探测、
  _log_prompt_body 关闭早退 / 开启落盘脱敏 / disabled 早退、_sync_diag_handlers
  同步 FileHandler、_log_final_payload hash 日志与序列化失败降级。

加载：importlib 唯一模块名装载（_diagnostics 模块级调用
_install_payload_diag_hook，默认关闭零副作用）。
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent


def _load_module(name: str, filename: str) -> Any:
    """按文件加载模块（唯一模块名，进程内缓存）。"""
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _PLUGIN_DIR / filename)
    assert spec is not None and spec.loader is not None, f"cannot load {filename}"
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def payload_diag() -> Any:
    return _load_module("llm_core_payload_diag_edges_under_test", "_payload_diag.py")


@pytest.fixture
def diagnostics() -> Any:
    return _load_module("llm_core_diagnostics_edges_under_test", "_diagnostics.py")


# ─────────────────── _payload_diag ───────────────────


class TestPayloadDiag:
    def test_redact_payload_recursive(self, payload_diag) -> None:
        """脱敏：敏感键整值替换（大小写无关、递归、list 逐项）。"""
        body = {
            "api_key": "sk-secret-123",
            "Authorization": "Bearer tok",
            "messages": [{"role": "user", "content": "hi", "x-api-key": "k"}],
            "temperature": 0.7,
            "nested": {"Key": "v", "ok": 1},
        }
        redacted = payload_diag.redact_payload(body)
        assert redacted["api_key"] == "***REDACTED***"
        assert redacted["Authorization"] == "***REDACTED***"
        assert redacted["messages"][0]["x-api-key"] == "***REDACTED***"
        assert redacted["nested"]["Key"] == "***REDACTED***"
        assert redacted["temperature"] == 0.7
        assert redacted["nested"]["ok"] == 1
        # 原 body 不被修改（深拷贝语义）
        assert body["api_key"] == "sk-secret-123"

    def test_redact_payload_scalar_passthrough(self, payload_diag) -> None:
        """非 dict/list 原样返回。"""
        assert payload_diag.redact_payload("plain") == "plain"
        assert payload_diag.redact_payload(42) == 42

    def test_safe_filename_segment(self, payload_diag) -> None:
        """文件名安全化：非字母数字/连字符/下划线 → 下划线；超长截断；空 → model。"""
        assert payload_diag._safe_filename_segment("deepseek-v4 pro") == "deepseek-v4_pro"
        assert payload_diag._safe_filename_segment("a" * 100) == "a" * 48
        assert payload_diag._safe_filename_segment("") == "model"

    def test_dump_disabled_returns_none(self, payload_diag, monkeypatch) -> None:
        """默认关闭 → 不落盘返回 None。"""
        monkeypatch.delenv("AGENTOS_PAYLOAD_DIAG", raising=False)
        assert payload_diag.dump_payload_diag("m", {"messages": []}) is None

    def test_dump_enabled_writes_redacted_tempfile(self, payload_diag, monkeypatch, tmp_path) -> None:
        """开启 → 写系统 tempfile，内容脱敏且可解析。"""
        monkeypatch.setenv("AGENTOS_PAYLOAD_DIAG", "1")
        monkeypatch.setattr(payload_diag.tempfile, "tempdir", str(tmp_path))
        path = payload_diag.dump_payload_diag("deepseek-v4", {"api_key": "sk-x", "messages": []})
        assert path is not None
        assert path.exists()
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["api_key"] == "***REDACTED***"
        assert "sk-x" not in path.read_text(encoding="utf-8")

    def test_dump_write_failure_returns_none(self, payload_diag, monkeypatch) -> None:
        """写盘失败（mkstemp 抛 OSError）→ warning + None（降级不阻断）。"""
        monkeypatch.setenv("AGENTOS_PAYLOAD_DIAG", "1")

        def _raise_oserror(*args: Any, **kwargs: Any) -> Any:
            raise OSError("disk full")

        monkeypatch.setattr(payload_diag.tempfile, "mkstemp", _raise_oserror)
        assert payload_diag.dump_payload_diag("m", {"messages": []}) is None


# ─────────────────── _diagnostics 纯函数 ───────────────────


class TestDiagnostics:
    def test_redact_prompt_patterns(self, diagnostics) -> None:
        """脱敏：sk- key / Bearer token / api_key 值三种形态。"""
        text = 'sk-abc123456789, Bearer tok123456789, "api_key": "sk-zzz999"}'
        redacted = diagnostics._redact_prompt(text)
        assert "sk-abc1..." in redacted
        assert "sk-abc123456789" not in redacted
        assert "Bearer tok123..." in redacted
        assert "tok123456789" not in redacted
        assert '"api_key": "***"' in redacted
        assert "sk-zzz999" not in redacted

    def test_resolve_prompt_log_path_finds_data_logs(self, diagnostics, monkeypatch, tmp_path) -> None:
        """向上探测 data/logs 目录 → 返回 prompt_audit.log 路径。"""
        logs_dir = tmp_path / "data" / "logs"
        logs_dir.mkdir(parents=True)
        monkeypatch.chdir(tmp_path)
        assert diagnostics._resolve_prompt_log_path() == str(logs_dir / "prompt_audit.log")

    def test_resolve_prompt_log_path_fallback_cwd(self, diagnostics, monkeypatch, tmp_path) -> None:
        """找不到 data/logs → 回退 cwd 下路径。"""
        monkeypatch.chdir(tmp_path)
        assert diagnostics._resolve_prompt_log_path() == str(tmp_path / "data" / "logs" / "prompt_audit.log")

    def test_log_prompt_body_disabled_returns_early(self, diagnostics, monkeypatch) -> None:
        """审计开关关闭 → 零副作用直接返回（不创建文件 handler）。"""
        monkeypatch.setattr(diagnostics, "_PROMPT_AUDIT_ENABLED", False)
        diagnostics._log_prompt_body("m", [{"role": "user", "content": "hi"}], None, api_key="sk-x")
        assert not any(
            isinstance(h, logging.handlers.RotatingFileHandler)
            for h in diagnostics._prompt_logger.handlers
        )

    def test_log_prompt_body_enabled_writes_redacted_file(
        self, diagnostics, monkeypatch, tmp_path
    ) -> None:
        """审计开启 → 写独立文件，api_key 脱敏。"""
        monkeypatch.setattr(diagnostics, "_PROMPT_AUDIT_ENABLED", True)
        # pytest 日志插件会给 logger 挂 LogCaptureHandler，导致 _sync_prompt_handlers
        # 幂等早退不建文件 handler——清空后让生产路径自建
        monkeypatch.setattr(diagnostics._prompt_logger, "handlers", [])
        log_file = tmp_path / "prompt_audit.log"
        monkeypatch.setenv("AGENTOS_LOG_PROMPT_FILE", str(log_file))
        diagnostics._log_prompt_body(
            "m",
            [{"role": "user", "content": "hi"}],
            None,
            api_key="sk-secret-1",
            temperature=0.7,
        )
        content = log_file.read_text(encoding="utf-8")
        assert "PROMPT" in content
        assert "sk-secret-1" not in content
        assert '"api_key": "***"' in content  # JSON 形态 api_key 值脱敏

    def test_log_prompt_body_disabled_logger_returns_early(self, diagnostics, monkeypatch, tmp_path) -> None:
        """logger 被 disabled（路径不可写降级）→ 早退不写审计记录。"""
        monkeypatch.setattr(diagnostics, "_PROMPT_AUDIT_ENABLED", True)
        monkeypatch.setattr(diagnostics._prompt_logger, "disabled", True)
        monkeypatch.setattr(diagnostics._prompt_logger, "handlers", [])
        log_file = tmp_path / "prompt_audit.log"
        monkeypatch.setenv("AGENTOS_LOG_PROMPT_FILE", str(log_file))
        diagnostics._log_prompt_body("m", [{"role": "user", "content": "hi"}], None)
        # disabled 早退在 handler 创建之后、写记录之前：文件可能被创建但无 PROMPT 记录
        content = log_file.read_text(encoding="utf-8") if log_file.exists() else ""
        assert "PROMPT" not in content

    def test_sync_diag_handlers_copies_file_handler(self, diagnostics, monkeypatch, tmp_path) -> None:
        """父 logger 的 FileHandler 同步到 _diag_logger（幂等：已挂则跳过）。"""
        handler = logging.FileHandler(tmp_path / "diag.log", encoding="utf-8")
        monkeypatch.setattr(diagnostics.logger, "handlers", [handler])
        monkeypatch.setattr(diagnostics._diag_logger, "handlers", [])
        diagnostics._sync_diag_handlers()
        assert diagnostics._diag_logger.handlers == [handler]
        # 幂等：再次调用直接返回
        diagnostics._sync_diag_handlers()
        assert diagnostics._diag_logger.handlers == [handler]

    def test_log_final_payload_hashes_and_logs(self, diagnostics, monkeypatch) -> None:
        """payload 诊断：hash 日志 + 落盘（受环境开关）。"""
        records: list[tuple[Any, ...]] = []

        class _FakeLogger:
            def info(self, *args: Any, **kwargs: Any) -> None:
                records.append(args)

        monkeypatch.setattr(diagnostics, "_DIAG_PAYLOAD_LOGGER", _FakeLogger())
        monkeypatch.setattr(diagnostics, "dump_payload_diag", lambda model, body: None)
        diagnostics._log_final_payload(
            "m", {"messages": [{"role": "user", "content": "hi"}], "temperature": 0.7}
        )
        assert records
        assert "POST_TRANSFORM" in records[0][0]
        assert any("POST_TRANSFORM_MSG" in r[0] for r in records)

    def test_log_final_payload_serialization_failure_degraded(self, diagnostics, monkeypatch) -> None:
        """body 含不可序列化值 → 收窄异常降级（不阻断主调用）。"""
        monkeypatch.setattr(diagnostics, "_DIAG_PAYLOAD_LOGGER", _FakeInfoLogger())
        monkeypatch.setattr(diagnostics, "dump_payload_diag", lambda model, body: None)
        diagnostics._log_final_payload("m", {"messages": [{"role": "user", "content": {1, 2}}]})
        # 不抛异常即通过（set 不可 JSON 序列化 → TypeError 被收窄捕获）

    def test_sync_prompt_handlers_idempotent(self, diagnostics, monkeypatch) -> None:
        """_sync_prompt_handlers 已挂 handler → 幂等早退。"""
        monkeypatch.setattr(diagnostics._prompt_logger, "handlers", [object()])
        diagnostics._sync_prompt_handlers()
        # 不抛异常即通过（早退分支）

    def test_sync_prompt_handlers_oserror_disables_logger(self, diagnostics, monkeypatch) -> None:
        """路径不可写（makedirs 抛 OSError）→ 静默降级 disabled=True。"""
        monkeypatch.setattr(diagnostics._prompt_logger, "handlers", [])
        monkeypatch.setattr(diagnostics._prompt_logger, "disabled", False)

        def _raise_oserror(*args: Any, **kwargs: Any) -> None:
            raise OSError("permission denied")

        monkeypatch.setattr(diagnostics.os, "makedirs", _raise_oserror)
        diagnostics._sync_prompt_handlers()
        assert diagnostics._prompt_logger.disabled is True

    def test_resolve_prompt_log_path_breaks_at_root(self, diagnostics, monkeypatch) -> None:
        """向上探测到文件系统根（parent == cwd）→ 停止并回退 cwd 路径。"""
        monkeypatch.setattr(diagnostics.os, "getcwd", lambda: "X:\\")
        assert diagnostics._resolve_prompt_log_path() == "X:\\data\\logs\\prompt_audit.log"

    async def test_install_payload_diag_hook_patches_transformation_classes(
        self, diagnostics, monkeypatch
    ) -> None:
        """开启诊断 → 拦截钩子包装 transform_request（同步+异步），幂等去重。"""
        import types

        monkeypatch.setenv("AGENTOS_PAYLOAD_DIAG", "1")
        monkeypatch.setattr(diagnostics, "dump_payload_diag", lambda model, body: None)
        monkeypatch.setattr(diagnostics, "_DIAG_PAYLOAD_LOGGER", _FakeInfoLogger())
        calls: list[str] = []

        class _FakeTransform:
            def transform_request(self, model, messages, optional_params, litellm_params, headers):  # noqa: ANN001
                calls.append("sync")
                return {"messages": messages}

            async def async_transform_request(self, model, messages, optional_params, litellm_params, headers):  # noqa: ANN001
                calls.append("async")
                return {"messages": messages}

        class _NoTransform:
            pass

        fake_mod = types.ModuleType("fake_gpt_transformation")
        fake_mod.TransformClass = _FakeTransform
        fake_mod.TransformClassDup = _FakeTransform  # 同 class 二次出现 → 幂等去重
        fake_mod.NoTransform = _NoTransform  # 无 transform_request → 跳过
        monkeypatch.setitem(
            sys.modules, "litellm.llms.openai.chat.gpt_transformation", fake_mod
        )

        diagnostics._install_payload_diag_hook()

        # 包装后调用：同步与异步方法都经 _log_final_payload 转发
        out = _FakeTransform().transform_request("m", [{"role": "user", "content": "hi"}], {}, {}, {})
        assert out == {"messages": [{"role": "user", "content": "hi"}]}
        assert calls == ["sync"]
        # 异步包装体：await 后返回 body 且经 _log_final_payload 转发
        async_out = await _FakeTransform().async_transform_request(
            "m", [{"role": "user", "content": "hi"}], {}, {}, {}
        )
        assert async_out == {"messages": [{"role": "user", "content": "hi"}]}
        assert calls == ["sync", "async"]

    def test_install_payload_diag_hook_import_error_skipped(self, diagnostics, monkeypatch) -> None:
        """transformation 模块 import 失败 → 跳过该模块（不阻断安装）。"""
        import importlib as _importlib

        monkeypatch.setenv("AGENTOS_PAYLOAD_DIAG", "1")
        monkeypatch.setattr(diagnostics, "dump_payload_diag", lambda model, body: None)
        monkeypatch.setattr(diagnostics, "_DIAG_PAYLOAD_LOGGER", _FakeInfoLogger())

        # 全部 transformation 模块 import 失败：跳过所有模块，不触发 litellm 真实导入
        def _fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
            if name.startswith("litellm.llms."):
                raise ImportError("module not available")
            return _importlib.import_module(name, *args, **kwargs)

        monkeypatch.setattr(_importlib, "import_module", _fake_import)
        diagnostics._install_payload_diag_hook()
        # 不抛异常即通过（ImportError 被收窄捕获并跳过）


class _FakeInfoLogger:
    def info(self, *args: Any, **kwargs: Any) -> None:
        pass
