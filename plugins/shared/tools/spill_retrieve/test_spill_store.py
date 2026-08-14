# @feature: FP-0.2.spill_guard 取回工具 | @vision: V1 可进化 | @ci: python-plugins-test
"""spill_retrieve TDD 测试——按 tool_call_id 读回 spill 原文 + 管道结束清理。

验证内容（与 spill_guard Rust 侧 spill_store.rs 契约对齐）：
1. test_sanitize_key_blocks_traversal —— key 消毒（分隔符/../纯点号）
2. test_read_spill_plain —— 明文存档读回
3. test_read_spill_gzip_magic_autodetect —— gzip magic 自动解压（无需配置协商）
4. test_read_spill_utf8 —— 中文/emoji 完整读回
5. test_read_spill_missing —— 不存在 → found=False（不抛异常）
6. test_cleanup_pipeline_removes_dir_only —— 只清目标 pipeline，幂等
7. test_resolve_base_path_env_override —— AGENTOS_SPILL_BASE 显式覆盖
8. test_resolve_base_path_absolute_passthrough —— 绝对路径直通
9. test_resolve_base_path_relative_to_project_root —— 相对路径锚定项目根
10. test_retrieve_tool_handler —— server 侧 handler 组装（_call_context 兜底 pipeline_id）
11. test_on_pipeline_end_cleanup —— on_pipeline_end 钩子清理目录

模块经 importlib 直接加载（同 test_tool.py 模式），不依赖运行中的 sidecar。
"""

from __future__ import annotations

import gzip
import importlib.util
import sys
from pathlib import Path

_TOOLS_ROOT = Path(__file__).resolve().parent
_SDK_DIR = Path(__file__).resolve().parents[4] / "plugins" / "sdk" / "src"


def _load(name: str, path: Path):
    if str(_TOOLS_ROOT) not in sys.path:
        sys.path.insert(0, str(_TOOLS_ROOT))
    if str(_SDK_DIR) not in sys.path:
        sys.path.insert(0, str(_SDK_DIR))
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


spill_store = _load("spill_store_py", _TOOLS_ROOT / "spill_store.py")


# ── sanitize_key ────────────────────────────────────────────────

def test_sanitize_key_blocks_traversal():
    assert spill_store.sanitize_key("call_abc") == "call_abc"
    assert spill_store.sanitize_key("call-123.x") == "call-123.x"
    evil = spill_store.sanitize_key("../../etc/passwd")
    assert "/" not in evil and "\\" not in evil and ".." not in evil
    assert spill_store.sanitize_key("///") != ""


# ── read_spill ──────────────────────────────────────────────────

def test_read_spill_plain(tmp_path):
    (tmp_path / "pipe-1").mkdir()
    (tmp_path / "pipe-1" / "call_a").write_text("hello 原文", encoding="utf-8")
    r = spill_store.read_spill(tmp_path, "pipe-1", "call_a")
    assert r["found"] is True
    assert r["content"] == "hello 原文"
    assert r["encoding"] == "plain"
    assert r["size_bytes"] == len("hello 原文".encode("utf-8"))


def test_read_spill_gzip_magic_autodetect(tmp_path):
    raw = "repeat " * 500
    d = tmp_path / "p"
    d.mkdir()
    (d / "call_g").write_bytes(gzip.compress(raw.encode("utf-8"), 6))
    r = spill_store.read_spill(tmp_path, "p", "call_g")
    assert r["found"] is True
    assert r["content"] == raw
    assert r["encoding"] == "gzip"


def test_read_spill_utf8(tmp_path):
    text = "中文日志\n" + "🙂" * 50
    d = tmp_path / "p"
    d.mkdir()
    (d / "k").write_bytes(gzip.compress(text.encode("utf-8")))
    r = spill_store.read_spill(tmp_path, "p", "k")
    assert r["content"] == text


def test_read_spill_missing(tmp_path):
    r = spill_store.read_spill(tmp_path, "nope", "nope")
    assert r["found"] is False
    assert "error" in r


# ── cleanup ─────────────────────────────────────────────────────

def test_cleanup_pipeline_removes_dir_only(tmp_path):
    for pipe, key in [("a", "k1"), ("a", "k2"), ("b", "k3")]:
        d = tmp_path / pipe
        d.mkdir(exist_ok=True)
        (d / key).write_text("x", encoding="utf-8")
    removed = spill_store.cleanup_pipeline(tmp_path, "a")
    assert removed == 2
    assert not (tmp_path / "a").exists()
    assert (tmp_path / "b" / "k3").exists()
    assert spill_store.cleanup_pipeline(tmp_path, "a") == 0  # 幂等


# ── resolve_base_path ───────────────────────────────────────────

def test_resolve_base_path_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTOS_SPILL_BASE", str(tmp_path / "env-spill"))
    assert spill_store.resolve_base_path("./data/spill") == tmp_path / "env-spill"


def test_resolve_base_path_absolute_passthrough(tmp_path):
    assert spill_store.resolve_base_path(str(tmp_path)) == tmp_path


def test_resolve_base_path_relative_to_project_root():
    # 相对路径锚定项目根（从本文件向上找含 config/ + plugins/ 的目录）
    p = spill_store.resolve_base_path("./data/spill")
    assert p.is_absolute()
    assert (p.parent).exists()  # 项目根存在
    assert (Path(__file__).resolve().parents[4] / "data") == p.parent


# ── server 侧 handler ───────────────────────────────────────────

def test_retrieve_tool_handler(tmp_path, monkeypatch):
    server = _load("spill_retrieve_server", _TOOLS_ROOT / "server.py")
    # 存档一份原文
    d = tmp_path / "pipe-ctx"
    d.mkdir()
    (d / "call_r9").write_text("full original text\n" * 10, encoding="utf-8")
    # handler 经 _call_context 拿 pipeline_id（param_inject 未注入 args 的兜底）
    result = server.spill_retrieve(
        tool_call_id="call_r9",
        pipeline_id="pipe-ctx",
        _spill_base=str(tmp_path),
        _call_context={"pipeline_id": "pipe-ctx"},
    )
    assert result["success"] is True
    data = result["data"]
    assert data["tool_call_id"] == "call_r9"
    assert "full original text" in data["content"]
    assert data["size_bytes"] > 0


def test_retrieve_tool_handler_missing(tmp_path):
    server = _load("spill_retrieve_server2", _TOOLS_ROOT / "server.py")
    result = server.spill_retrieve(
        tool_call_id="ghost",
        pipeline_id="p",
        _spill_base=str(tmp_path),
    )
    # 不存在：失败结果（工具契约），但不崩溃
    assert result["success"] is False


def test_on_pipeline_end_cleanup(tmp_path, monkeypatch):
    server = _load("spill_retrieve_server3", _TOOLS_ROOT / "server.py")
    d = tmp_path / "pipe-end"
    d.mkdir()
    (d / "k1").write_text("x", encoding="utf-8")
    (d / "k2").write_text("y", encoding="utf-8")
    # on_pipeline_end 钩子（内核 notifications/on_pipeline_end → SDK 分发）
    server._handle_pipeline_end({"pipeline_id": "pipe-end", "_spill_base": str(tmp_path)})
    assert not d.exists()
