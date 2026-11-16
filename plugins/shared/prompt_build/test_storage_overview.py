# @feature: FP-0.2.〇 管道引擎 | @vision: V3 可嵌入 | @ci: none-local
"""{{storage_overview}} 本机存储实时盘点占位符单测。

背景（2026-10-22）：执行者 agent 逐盘符盲探（J/K/L/X/Y/Z 逐个试错）是已观测
的 token/时延浪费；挂载会静默消失（WinFsp 远端盘掉线实证），静态清单会撒谎
——盘点在系统提示词组装时现查（会话粒度实时）。

契约：
    - 解析：storage_overview 是无参保留名
    - 渲染：逐盘符一行（类型/卷标/容量），防盲探指令在位，空条目=空串零注入
    - 解析降级：采集失败=空串（不阻断提示词构建，与模式物料降级同口径）
    - 真枚举（Windows 实机）：产出含 C 盘的盘点文本

原则：只通过公开/受保护方法断言可观察行为；采集函数 monkeypatch，不 mock 内部。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

# 复制 server.py 的 sys.path 机制：插件目录（本地 plugin.py）+ plugins/shared/（pipeline 包）
_THIS_DIR = str(Path(__file__).resolve().parent)
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)
_SHARED_DIR = str(Path(__file__).resolve().parents[1])  # plugins/shared/
if _SHARED_DIR not in sys.path:
    sys.path.insert(0, _SHARED_DIR)

from _libs.pipeline_base.plugin import PluginContext  # noqa: E402

# 全车道共跑时裸名 `plugin` 会被先收集目录的同名模块劫持，
# 按 _THIS_DIR 显式路径加载（与 test_parse_placeholder.py 同范式）。
_spec = importlib.util.spec_from_file_location(
    "prompt_build_plugin_storage_test", str(Path(_THIS_DIR) / "plugin.py")
)
assert _spec is not None
assert _spec.loader is not None
_mod = importlib.util.module_from_spec(_spec)
sys.modules["prompt_build_plugin_storage_test"] = _mod
_spec.loader.exec_module(_mod)
PromptBuildPlugin: Any = _mod.PromptBuildPlugin  # noqa: E402

GB = 1024**3


def make_plugin() -> PromptBuildPlugin:
    return PromptBuildPlugin({})


def make_ctx(state: dict[str, Any] | None = None) -> PluginContext:
    return PluginContext(state=dict(state or {}))


def _run(coro: Any) -> Any:
    """同步执行协程（新建事件循环，避免 pytest-asyncio 冲突）。"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class TestParseStorageOverview:
    def test_parse_is_reserved_name(self) -> None:
        """storage_overview 是无参保留名，返回 (名称, 空参数字典)。"""
        assert make_plugin()._parse_placeholder("storage_overview") == ("storage_overview", {})


class TestRenderStorageOverview:
    def test_render_lines_with_type_label_size(self) -> None:
        text = _mod._render_storage_overview(
            [
                {
                    "root": "C:\\",
                    "type_label": "本地",
                    "label": "",
                    "free": 10 * GB,
                    "total": 100 * GB,
                },
                {
                    "root": "V:\\",
                    "type_label": "网络盘",
                    "label": "VM-0-16-ubuntu",
                    "free": GB,
                    "total": 80 * GB,
                },
            ]
        )
        assert "C:\\" in text
        assert "本地" in text
        assert "剩 10.0/100.0 GB" in text
        assert "网络盘" in text
        assert "卷标=VM-0-16-ubuntu" in text
        # 防逐盘符盲探指令在位（单行头，无散文段）
        assert "勿探测" in text
        assert text.count("\n") == 2  # 1 头 + 2 盘行，零废话

    def test_render_missing_enhanced_fields(self) -> None:
        """卷标/容量缺失（探测失败的盘）只少该项，盘符行保留。"""
        text = _mod._render_storage_overview(
            [{"root": "/", "type_label": "本地", "label": "", "free": None, "total": None}]
        )
        assert "剩 " not in text
        assert "卷标" not in text
        assert "- / 本地" in text

    def test_render_empty_entries_zero_injection(self) -> None:
        """空条目=空串（合法形态零注入，与模式目录空态同口径）。"""
        assert _mod._render_storage_overview([]) == ""


class TestParseProcMounts:
    """POSIX 多挂载点解析（云端/Linux 形态：伪 fs 过滤、实盘去重、远端标注）。"""

    def test_filters_pseudo_keeps_real_and_root(self) -> None:
        mounts = "\n".join(
            [
                "/dev/vda1 / ext4 rw 0 0",
                "proc /proc proc rw 0 0",
                "tmpfs /dev/shm tmpfs rw 0 0",
                "overlay /mnt/docker overlay rw 0 0",
                "/dev/vda2 /boot ext4 rw 0 0",
                "/dev/vdb1 /data ext4 rw 0 0",
                "/dev/vdb1 /data2 ext4 rw 0 0",
                "nas:/vol /mnt/nas nfs4 rw 0 0",
                "//box/share /mnt/smb cifs rw 0 0",
                "/dev/vda1 /etc/hosts ext4 rw 0 0",
            ]
        )
        entries = _mod._parse_proc_mounts(mounts)
        roots = [e["root"] for e in entries]
        # / 恒首位；伪 fs（proc/tmpfs/overlay 非 / 位）剔除；bind 噪声与同盘重复剔除
        assert roots[0] == "/"
        assert "/boot" in roots
        assert "/data" in roots
        assert "/proc" not in roots
        assert "/dev/shm" not in roots
        assert "/mnt/docker" not in roots
        assert "/etc/hosts" not in roots
        assert "/data2" not in roots  # /dev/vdb1 设备去重（/data 保留，同盘后挂载丢弃）
        remote = {e["root"]: e for e in entries}
        assert remote["/mnt/nas"]["type_label"] == "网络盘"
        assert remote["/mnt/nas"]["label"] == "nas:/vol"
        assert remote["/mnt/smb"]["type_label"] == "网络盘"
        assert remote["/data"]["type_label"] == "本地"

    def test_container_root_overlay_kept(self) -> None:
        """容器形态：/ 为 overlay 仍保底收录（agent 永远有基准盘）。"""
        entries = _mod._parse_proc_mounts("overlay / overlay rw 0 0\nproc /proc proc rw 0 0\n")
        assert [e["root"] for e in entries] == ["/"]
        assert entries[0]["type_label"] == "本地"

    def test_missing_mounts_file_fallback_single_root(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """无 /proc/mounts（非 Linux POSIX）退单根，不空转。"""
        monkeypatch.setattr(_mod, "_POSIX_MOUNTS_PATH", "/nonexistent/proc/mounts")
        entries = _mod._posix_drive_entries()
        assert [e["root"] for e in entries] == ["/"]
        assert entries[0]["type_label"] == "本地"

    def test_posix_entries_reads_mounts_and_stats(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """端到端：读挂载表 + 逐挂载 statvfs（二者皆注入桩）。"""
        import os as _os

        mounts = "/dev/vda1 / ext4 rw 0 0\nnas:/vol /mnt/nas nfs4 rw 0 0\n"
        monkeypatch.setattr(_mod, "_POSIX_MOUNTS_PATH", "/fake/proc/mounts")
        monkeypatch.setattr(Path, "read_text", lambda *_a, **_k: mounts)

        class FakeStat:
            f_bavail = 2 * 1024 * 1024  # 2 MiB units → f_frsize 512 → 1 GiB
            f_blocks = 4 * 1024 * 1024
            f_frsize = 512

        monkeypatch.setattr(_os, "statvfs", lambda *_a, **_k: FakeStat(), raising=False)
        entries = _mod._posix_drive_entries()
        by_root = {e["root"]: e for e in entries}
        assert by_root["/"]["total"] == 4 * 1024 * 1024 * 512
        assert by_root["/mnt/nas"]["free"] == 2 * 1024 * 1024 * 512
        assert by_root["/mnt/nas"]["type_label"] == "网络盘"


class TestResolveStorageOverview:
    def test_resolve_expands_collected_text(self) -> None:
        plugin = make_plugin()
        orig = _mod._collect_storage_overview
        _mod._collect_storage_overview = lambda: "- V:\\ 网络盘"
        try:
            out = _run(plugin._resolve_placeholder(make_ctx(), "storage_overview"))
        finally:
            _mod._collect_storage_overview = orig
        assert out == "- V:\\ 网络盘"

    def test_resolve_degrade_to_empty_on_error(self) -> None:
        """采集失败=空串（fail-soft 不阻断提示词构建）。"""
        def boom() -> str:
            raise OSError("枚举失败")

        plugin = make_plugin()
        orig = _mod._collect_storage_overview
        _mod._collect_storage_overview = boom
        try:
            out = _run(plugin._resolve_placeholder(make_ctx(), "storage_overview"))
        finally:
            _mod._collect_storage_overview = orig
        assert out == ""


@pytest.mark.skipif(sys.platform != "win32", reason="Windows 盘符真枚举")
class TestRealWindowsEnumeration:
    def test_enumeration_contains_system_drive(self) -> None:
        text = _run(make_plugin()._resolve_placeholder(make_ctx(), "storage_overview"))
        assert "C:\\" in text
        assert "本地" in text
