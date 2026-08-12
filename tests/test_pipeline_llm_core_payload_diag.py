"""P0-2 回归测试：诊断钩子默认不落盘原始 body，api_key 永不出现在落盘内容。

背景：``adapter._install_payload_diag_hook()`` 在模块加载期无条件 monkey-patch
litellm，每次 LLM 调用把原始 HTTP body（含明文 api_key / Authorization 与完整
prompt）落盘到 ``logs/payload_diag/``，且用 ``except Exception: pass`` 空吞错。

修复策略（与生产代码 ``_payload_diag.py`` 对齐）：
- 默认关闭（``AGENTOS_PAYLOAD_DIAG != "1"``）→ 不 patch、不落盘；
- 开启时写系统 tempfile（不再污染仓库目录），且写入前对敏感字段脱敏；
- 脱敏逻辑是纯函数、不依赖 litellm，便于此处单测。

测试断言行为（WHY）：默认不落盘；即使开启，密钥也不得出现在落盘内容里。
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parent.parent
_LLM_CORE_DIR = _REPO_ROOT / "plugins" / "shared" / "pipeline" / "core" / "llm_core"


def _load_payload_diag():
    """按文件加载 ``_payload_diag.py``（litellm 无关），返回全新模块实例。

    每次重新加载，避免模块级状态/env 缓存跨用例污染。
    """
    mod_path = _LLM_CORE_DIR / "_payload_diag.py"
    assert mod_path.exists(), f"_payload_diag.py missing at {mod_path}"
    mod_name = "_payload_diag_p0_test"
    sys.modules.pop(mod_name, None)
    spec = importlib.util.spec_from_file_location(mod_name, mod_path)
    assert spec is not None and spec.loader is not None, f"Cannot load {mod_path}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def test_payload_diag_disabled_by_default(monkeypatch, tmp_path) -> None:
    """默认（env 未设）不得落盘任何原始 body。"""
    monkeypatch.delenv("AGENTOS_PAYLOAD_DIAG", raising=False)
    monkeypatch.chdir(tmp_path)
    mod = _load_payload_diag()

    assert mod.is_payload_diag_enabled() is False

    body = {"model": "m", "api_key": "sk-SECRET", "messages": [{"role": "user", "content": "hi"}]}
    result = mod.dump_payload_diag("m", body)

    assert result is None, "默认关闭时不应返回写入路径"
    # 当前工作目录（仓库内）下不应产生任何诊断文件
    assert list(tmp_path.rglob("*.json")) == [], "默认关闭时不得落盘"


def test_redact_payload_strips_secrets() -> None:
    """脱敏必须覆盖 api_key / Authorization 及嵌套结构，且保留非敏感内容。"""
    mod = _load_payload_diag()
    body = {
        "model": "m",
        "api_key": "sk-SECRET",
        "Authorization": "Bearer ABCDEF",
        "headers": {"x-api-key": "sk-NESTED", "content-type": "application/json"},
        "messages": [{"role": "user", "content": "please-keep-me"}],
    }
    dumped = json.dumps(mod.redact_payload(body))

    # 密钥不得出现在脱敏后的序列化结果里
    assert "sk-SECRET" not in dumped
    assert "Bearer ABCDEF" not in dumped
    assert "ABCDEF" not in dumped
    assert "sk-NESTED" not in dumped
    # 非敏感业务内容必须保留（脱敏不能误伤 payload 结构）
    assert "please-keep-me" in dumped
    assert "application/json" in dumped


def test_dump_when_enabled_uses_tempfile_and_redacts(monkeypatch) -> None:
    """开启时写系统 tempfile（不在仓库内），且落盘内容已脱敏 api_key。"""
    monkeypatch.setenv("AGENTOS_PAYLOAD_DIAG", "1")
    mod = _load_payload_diag()
    assert mod.is_payload_diag_enabled() is True

    body = {"model": "m", "api_key": "sk-SECRET", "messages": [{"role": "user", "content": "hi"}]}
    path = mod.dump_payload_diag("m", body)

    assert path is not None, "开启时应写入 tempfile"
    try:
        content = path.read_text(encoding="utf-8")
        assert "sk-SECRET" not in content, "落盘内容不得含明文 api_key"
        # 写到系统临时目录，而非仓库内的 logs/payload_diag（安全目标：不污染仓库、不落盘到源码树）
        assert str(path).startswith(tempfile.gettempdir()), "应写入系统 tempfile，不污染仓库"
        assert not _is_within_repo(path), "不得写回仓库目录（含 logs/payload_diag）"
    finally:
        os.unlink(path)


def _is_within_repo(p: Path) -> bool:
    """判断路径是否位于仓库根目录之内。"""
    try:
        p.resolve().relative_to(_REPO_ROOT.resolve())
        return True
    except ValueError:
        return False
