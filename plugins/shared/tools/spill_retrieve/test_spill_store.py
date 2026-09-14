# @feature: FP-0.2.spill_guard 取回工具 | @vision: V1 可进化 | @ci: none-local
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
    assert spec is not None and spec.loader is not None
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
    assert r["size_bytes"] == len("hello 原文".encode())


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


# ── G 簇缺口补测（2026-09-14 coverage 缺行）──────────────────────


def test_infer_project_root_returns_none_when_no_layout(monkeypatch):
    """_infer_project_root：向上找不到含 config/+plugins/ 的目录 → None（63）。

    把模块 __file__ 指向无 0.2 布局的隔离路径后父链穷尽即 None；对照组用
    真实文件位置验证正常推导非恒 None（防常量返回）。
    """
    fake = Path("C:/nonexistent/isolated/spill_store.py")
    monkeypatch.setattr(spill_store, "__file__", str(fake))
    assert spill_store._infer_project_root() is None
    monkeypatch.undo()
    assert spill_store._infer_project_root() is not None


def test_resolve_base_path_falls_back_to_cwd_when_root_unknown(monkeypatch, tmp_path):
    """root 推导失败 → cwd / configured（54 的 else 分支，有区分度输入）。"""
    monkeypatch.setattr(spill_store, "_infer_project_root", lambda: None)
    monkeypatch.chdir(tmp_path)
    resolved = spill_store.resolve_base_path("./data/spill")
    assert resolved == Path.cwd() / "data/spill"


def test_resolve_base_path_blank_env_falls_through(monkeypatch, tmp_path):
    """AGENTOS_SPILL_BASE 为空白串 → 不算显式覆盖，继续走路径判定。"""
    monkeypatch.setenv("AGENTOS_SPILL_BASE", "   ")
    assert spill_store.resolve_base_path(str(tmp_path)) == tmp_path


def test_read_spill_unreadable_file_returns_error(tmp_path, monkeypatch):
    """read_bytes 抛 OSError → found=False + 读取失败（81-82）。"""
    d = tmp_path / "pipe"
    d.mkdir()
    target = d / "call_ro"
    target.write_text("x", encoding="utf-8")

    def _boom(self):
        raise OSError("device I/O error")

    monkeypatch.setattr(spill_store.Path, "read_bytes", _boom)
    r = spill_store.read_spill(tmp_path, "pipe", "call_ro")
    assert r["found"] is False
    assert "spill 读取失败" in r["error"]
    assert r["tool_call_id"] == "call_ro"


def test_read_spill_corrupt_gzip_returns_error(tmp_path):
    """gzip magic 但尾部损坏 → found=False + 解压失败（86-87）。

    破坏真实 gzip 产物的尾 5 字节（BadGzipFile 属 OSError），非伪造 magic。
    """
    d = tmp_path / "pipe"
    d.mkdir()
    corrupted = bytearray(gzip.compress(b"payload " * 50))
    corrupted[-5] ^= 0xFF
    (d / "call_bad").write_bytes(bytes(corrupted))
    r = spill_store.read_spill(tmp_path, "pipe", "call_bad")
    assert r["found"] is False
    assert "spill 解压失败" in r["error"]



def test_read_spill_gzip_non_utf8_payload_returns_error(tmp_path):
    """合法 gzip 但解压内容非 UTF-8 → 解压失败（86-87 的 UnicodeDecodeError 侧）。

    截断流抛 EOFError（不在捕获集内，属上游完整性错误），故用「结构合法、
    内容非 UTF-8」构造捕获集内的另一半路径。
    """
    d = tmp_path / "pipe"
    d.mkdir()
    (d / "call_nonutf8gz").write_bytes(gzip.compress(bytes([0xFF, 0xFE, 0x00, 0x01, 0x80, 0x81])))
    r = spill_store.read_spill(tmp_path, "pipe", "call_nonutf8gz")
    assert r["found"] is False
    assert "spill 解压失败" in r["error"]



def test_read_spill_non_utf8_returns_error(tmp_path):
    """非 gzip 且非 UTF-8 字节 → found=False + 非 UTF-8（92-93）。"""
    d = tmp_path / "pipe"
    d.mkdir()
    (d / "call_bin").write_bytes(b"\xff\xfe\x00\x01\x80\x81")
    r = spill_store.read_spill(tmp_path, "pipe", "call_bin")
    assert r["found"] is False
    assert "非 UTF-8" in r["error"]


def test_read_spill_empty_file_is_valid_empty_content(tmp_path):
    """空文件是合法 UTF-8（对照组：不落错误分支）。"""
    d = tmp_path / "pipe"
    d.mkdir()
    (d / "call_empty").write_bytes(b"")
    r = spill_store.read_spill(tmp_path, "pipe", "call_empty")
    assert r["found"] is True
    assert r["content"] == ""
    assert r["size_bytes"] == 0
    assert r["encoding"] == "plain"


# ── server 侧缺口 ───────────────────────────────────────────


def test_spill_config_non_dict_returns_empty(tmp_path):
    """_spill_config：配置项非 dict → {}（49）。"""
    server = _load("spill_retrieve_server_cfg", _TOOLS_ROOT / "server.py")
    original = server.plugin.get_config

    def _fake_config():
        return {"spill": "not-a-dict"}

    server.plugin.get_config = _fake_config
    try:
        assert server._spill_config() == {}
    finally:
        server.plugin.get_config = original


def test_spill_config_missing_namespace_returns_empty(tmp_path):
    """配置无 spill 段 → {}（有区分度输入）。"""
    server = _load("spill_retrieve_server_cfg2", _TOOLS_ROOT / "server.py")
    server.plugin.get_config = lambda: {}
    assert server._spill_config() == {}


def test_resolve_pipeline_id_priority_chain(tmp_path):
    """pipeline_id 解析优先级：显式 > _call_context > default（57-60）。"""
    server = _load("spill_retrieve_server_pid", _TOOLS_ROOT / "server.py")
    assert server._resolve_pipeline_id("explicit", {"_call_context": {"pipeline_id": "ctx"}}) == "explicit"
    assert server._resolve_pipeline_id("", {"_call_context": {"pipeline_id": "ctx"}}) == "ctx"
    assert server._resolve_pipeline_id("", {"_call_context": "not-a-dict"}) == "default"
    assert server._resolve_pipeline_id("", {}) == "default"
    assert server._resolve_pipeline_id("", {"_call_context": {"pipeline_id": ""}}) == "default"


def test_spill_retrieve_empty_tool_call_id_returns_error(tmp_path):
    """tool_call_id 为空 → 显式失败（74），不落基准目录解析。"""
    server = _load("spill_retrieve_server_empty", _TOOLS_ROOT / "server.py")
    result = server.spill_retrieve(tool_call_id="", pipeline_id="p", _spill_base=str(tmp_path))
    assert result["success"] is False
    assert "tool_call_id 不能为空" in result["error"]


def test_pipeline_end_cleanup_disabled_by_config(tmp_path):
    """cleanup_on_pipeline_end=False → 直接返回不清理（110）。"""
    server = _load("spill_retrieve_server_end", _TOOLS_ROOT / "server.py")
    server.plugin.get_config = lambda: {"spill": {"cleanup_on_pipeline_end": False}}
    d = tmp_path / "keep-me"
    d.mkdir()
    (d / "k").write_text("x", encoding="utf-8")
    server._handle_pipeline_end({"pipeline_id": "keep-me", "_spill_base": str(tmp_path)})
    assert d.exists()


def test_pipeline_end_blank_pipeline_id_noop(tmp_path):
    """pipeline_id 空白 → 直接返回（113），不误删 base 目录。"""
    server = _load("spill_retrieve_server_end2", _TOOLS_ROOT / "server.py")
    sentinel = tmp_path / "sentinel"
    sentinel.mkdir()
    (sentinel / "k").write_text("x", encoding="utf-8")
    server._handle_pipeline_end({"_spill_base": str(tmp_path)})
    server._handle_pipeline_end({"pipeline_id": "   ", "_spill_base": str(tmp_path)})
    assert sentinel.exists()


def test_main_entrypoint_invokes_plugin_run(monkeypatch):
    """main() 委托 plugin.run()（133）。"""
    server = _load("spill_retrieve_server_main", _TOOLS_ROOT / "server.py")
    called: list[bool] = []
    monkeypatch.setattr(server.plugin, "run", lambda: called.append(True))
    server.main()
    assert called == [True]
