# @feature: FP-MIGR 0.1→0.2迁移 | @vision: V3 可嵌入 | @ci: python-coverage
"""download 工具落位原子性测试（E3 跨平台覆盖语义统一）。

锁定契约：``_stream_download`` 完成后经 ``os.replace`` 落位——目标已存在时
覆盖为新内容（POSIX/Windows 语义一致）。行为变化（E3）：Windows 上重复下载
同名文件从 rename 必败（FileExistsError）变为覆盖成功。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_DL_DIR = Path(__file__).resolve().parents[4] / "plugins" / "shared" / "tools" / "download"


def _load_module() -> Any:
    mod_name = "download_tool_replace_test"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    _s = str(_DL_DIR)
    if _s in sys.path:
        sys.path.remove(_s)
    sys.path.insert(0, _s)
    for _m in ("workspace_aware", "tool", "url_security"):
        sys.modules.pop(_m, None)
    spec = importlib.util.spec_from_file_location(mod_name, _DL_DIR / "tool.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def mod() -> Any:
    return _load_module()


def _tool(mod: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """DownloadTool 实例（工作区路径闸放行，聚焦落位语义）。"""
    tool = mod.DownloadTool()
    monkeypatch.setattr(type(tool), "check_path_allowed", lambda self, path, operation="read", agent_level=None: (True, ""))
    return tool


class _FakeResponse:
    """httpx.Response 替身：200 + 两段字节流。"""

    status_code = 200

    def raise_for_status(self) -> None:
        return None

    async def aiter_bytes(self, chunk_size: int):
        yield b"NEW-CONTENT-"
        yield b"PART2"


class _FakeClient:
    async def request(self, method: str, url: str, **kwargs: Any) -> _FakeResponse:
        return _FakeResponse()


def test_finalize_completed_tmp_replaces_existing_final(
    mod: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """tmp 已完整 + final 已存在（重复下载）：replace 覆盖成功且内容为 tmp。"""
    final = tmp_path / "file.bin"
    final.write_bytes(b"OLD-CONTENT")
    tmp = Path(str(final) + ".tmp")
    tmp.write_bytes(b"COMPLETE-BODY")

    tool = _tool(mod, monkeypatch)
    result = asyncio_run(
        tool._stream_download(
            client=_FakeClient(), url="https://example.com/f.bin",
            final_path=final, state_path=tmp_path / "state.json",
            max_retries=1, max_size=0, content_length=len(b"COMPLETE-BODY"),
        )
    )
    assert result["path"] == final
    assert final.read_bytes() == b"COMPLETE-BODY"
    assert not tmp.exists()  # tmp 已原子让位为 final


def test_fresh_download_overwrites_existing_final(
    mod: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """全新下载但 final 已存在（Windows rename 必败场景）：覆盖为新下载内容。"""
    final = tmp_path / "file.bin"
    final.write_bytes(b"OLD-CONTENT")
    tool = _tool(mod, monkeypatch)

    result = asyncio_run(
        tool._stream_download(
            client=_FakeClient(), url="https://example.com/f.bin",
            final_path=final, state_path=tmp_path / "state.json",
            max_retries=1, max_size=0, content_length=-1,
        )
    )
    assert final.read_bytes() == b"NEW-CONTENT-PART2"
    assert result["size"] == len(b"NEW-CONTENT-PART2")


def asyncio_run(coro: Any) -> Any:
    import asyncio

    return asyncio.run(coro)
