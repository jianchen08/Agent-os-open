# @feature: FP-0.2.〇 TB参赛适配 | @ci: none-local
"""fs 工具外部容器路由测试（R11）。

绑定会话（guard 注入 _container_id/_container_workdir）下 fs 族原语进收养
容器执行；无绑定（_container_id=None）走 host 原路径（不回归对照）。
路径语义：容器绝对路径直通；相对路径锚绑定 workdir。传输 = docker exec
argv positional（host 侧无 shell）+ base64 stdin 写。_container_exec 桩经
monkeypatch（自动还原，不污染同进程其他测试的模块状态）。
"""

from __future__ import annotations

import base64
from typing import Any

import pytest

from agentos_builtin_tools import fs_tools

pytestmark = pytest.mark.unit


class _ExecRecorder:
    """_container_exec 记录型桩：脚本/args/stdin 全录，stdout 可配。

    test -e 存在性探测默认目标不存在（rc=1）——copy/move overwrite=False 通路。
    """

    def __init__(self, stdout: str = "", rc: int = 0) -> None:
        self.stdout = stdout
        self.rc = rc
        self.calls: list[dict[str, Any]] = []

    async def __call__(
        self,
        container_id: str,
        script: str,
        *,
        args: list[str] | None = None,
        stdin_data: str | None = None,
        timeout: float = 60.0,
    ) -> tuple[int, str, str]:
        self.calls.append(
            {
                "container_id": container_id,
                "script": script,
                "args": list(args or []),
                "stdin_data": stdin_data,
            }
        )
        if "test -e" in script:
            return 1, "", ""
        return self.rc, self.stdout, ("" if self.rc == 0 else "stub error")


class _RawExecRecorder:
    """_container_exec_raw 记录型桩：stdout 字节面（R14 起文件读取走 raw 面）。"""

    def __init__(self, stdout: bytes = b"", rc: int = 0) -> None:
        self.stdout = stdout
        self.rc = rc
        self.calls: list[dict[str, Any]] = []

    async def __call__(
        self,
        container_id: str,
        script: str,
        *,
        args: list[str] | None = None,
        stdin_data: str | None = None,
        timeout: float = 60.0,
    ) -> tuple[int, bytes, bytes]:
        self.calls.append(
            {
                "container_id": container_id,
                "script": script,
                "args": list(args or []),
                "stdin_data": stdin_data,
            }
        )
        if "test -e" in script:
            return 1, b"", b""
        return self.rc, self.stdout, (b"" if self.rc == 0 else b"stub error")


def _attach(monkeypatch: pytest.MonkeyPatch, recorder: _ExecRecorder) -> None:
    monkeypatch.setattr(fs_tools, "_container_exec", recorder)


def _attach_raw(monkeypatch: pytest.MonkeyPatch, recorder: _RawExecRecorder) -> None:
    monkeypatch.setattr(fs_tools, "_container_exec_raw", recorder)


def _run(coro: Any) -> Any:
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class TestContainerRead:
    def test_read_absolute_path_returns_content(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rec = _RawExecRecorder(stdout=b"hello\nworld\n")
        _attach_raw(monkeypatch, rec)
        result = _run(
            fs_tools.file_read(
                "/app/data/config.json", _container_id="tb-x", _container_workdir="/app"
            )
        )
        assert result.success is True
        expected = "hello" + chr(10) + "world"  # splitlines 语义：尾换行不进内容
        assert result.output["content"] == expected
        assert result.output["file"] == "/app/data/config.json"
        assert result.output["container"] == "tb-x"
        assert 'cat "$1"' in rec.calls[0]["script"]

    def test_read_relative_path_anchors_workdir(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rec = _RawExecRecorder(stdout=b"x")
        _attach_raw(monkeypatch, rec)
        result = _run(
            fs_tools.file_read(
                "out/result.txt", _container_id="tb-x", _container_workdir="/app"
            )
        )
        assert result.success is True
        assert rec.calls[0]["args"] == ["/app/out/result.txt"]

    def test_read_failure_carries_stderr(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rec = _RawExecRecorder(stdout=b"", rc=1)
        _attach_raw(monkeypatch, rec)
        result = _run(
            fs_tools.file_read("/nope", _container_id="tb-x", _container_workdir="/app")
        )
        assert result.success is False
        assert "stub error" in str(result.error)

    def test_line_range_client_slice(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rec = _RawExecRecorder(stdout=b"l1\nl2\nl3\nl4\n")
        _attach_raw(monkeypatch, rec)
        result = _run(
            fs_tools.file_read(
                "/f.txt", start_line=2, end_line=3, _container_id="tb-x", _container_workdir="/"
            )
        )
        assert result.output["content"] == "l2\nl3"


class TestContainerReadImageModality:
    """R14：容器分支图片模态分流（对齐宿主分支同款契约）。

    修前缺陷（TB 终跑 887b8135f4c5 实证）：容器内 PNG 经 utf-8 replace 解码成
    一屏乱码文本直吐模型——图片注入双重断裂的第二断点（第一断点=R13 结论：
    无任何图片 blob 入 LLM 请求层）。修后魔数命中 → base64_data+mime_type
    契约（tool_core collect_pending_images 消费键），非图片二进制干净拒绝，
    文本文件行为不回归。
    """

    # 最小合法 PNG 头（8 字节签名）+ IHDR 长度/类型 + 随体数据
    _PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + b"payload-bytes"
    _JPEG = b"\xff\xd8\xff\xe0" + b"jfif-payload"

    def test_png_returns_modality_contract(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rec = _RawExecRecorder(stdout=self._PNG)
        _attach_raw(monkeypatch, rec)
        result = _run(
            fs_tools.file_read(
                "/app/data/layout.png", _container_id="tb-x", _container_workdir="/app"
            )
        )
        assert result.success is True
        assert result.output["mime_type"] == "image/png"
        assert result.output["path"] == "/app/data/layout.png"
        assert result.output["size"] == len(self._PNG)
        assert result.output["container"] == "tb-x"
        # 契约核心：base64 无损还原原字节（非乱码文本）
        assert base64.b64decode(result.output["base64_data"]) == self._PNG
        # 模态结果不携带文本面字段（防两态混杂）
        assert "content" not in result.output
        assert "lines" not in result.output

    def test_jpeg_also_routed_to_modality(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """≥2 组有区分度输入：第二种图片魔数同样分流（防 PNG 特例拟合）。"""
        rec = _RawExecRecorder(stdout=self._JPEG)
        _attach_raw(monkeypatch, rec)
        result = _run(
            fs_tools.file_read("/app/pic.jpg", _container_id="tb-x", _container_workdir="/")
        )
        assert result.success is True
        assert result.output["mime_type"] == "image/jpeg"
        assert base64.b64decode(result.output["base64_data"]) == self._JPEG

    def test_non_image_binary_rejected_clean(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rec = _RawExecRecorder(stdout=b"\x7fELF\x02\x01\x01\x00" + b"\x00" * 16)
        _attach_raw(monkeypatch, rec)
        result = _run(
            fs_tools.file_read("/app/bin/tool", _container_id="tb-x", _container_workdir="/")
        )
        assert result.success is False
        assert "Binary file or encoding issue" in str(result.error)

    def test_oversized_image_rejected_clean(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(fs_tools, "_MAX_IMAGE_EMBED_BYTES", 8)
        rec = _RawExecRecorder(stdout=self._PNG + b"pad")
        _attach_raw(monkeypatch, rec)
        result = _run(
            fs_tools.file_read("/app/big.png", _container_id="tb-x", _container_workdir="/")
        )
        assert result.success is False
        assert "Image too large to inline" in str(result.error)


class TestContainerWrite:
    def test_write_base64_stdin_with_parent_dir(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rec = _ExecRecorder()
        _attach(monkeypatch, rec)
        result = _run(
            fs_tools.file_write(
                "/app/output/config.json",
                content='{"ok": true}',
                _container_id="tb-x",
                _container_workdir="/app",
            )
        )
        assert result.success is True
        assert result.output["container"] == "tb-x"
        call = rec.calls[-1]
        assert call["script"] == 'mkdir -p "$(dirname "$1")" && base64 -d > "$1"'
        assert call["args"] == ["/app/output/config.json"]
        decoded = base64.b64decode(call["stdin_data"]).decode("utf-8")
        assert decoded == '{"ok": true}'

    def test_append_reads_then_writes_merged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rec = _ExecRecorder(stdout="line1\n")
        _attach(monkeypatch, rec)
        result = _run(
            fs_tools.file_write(
                "/f.txt",
                action="append",
                content="line2\n",
                _container_id="tb-x",
                _container_workdir="/",
            )
        )
        assert result.success is True
        write_calls = [c for c in rec.calls if "base64 -d" in c["script"]]
        decoded = base64.b64decode(write_calls[-1]["stdin_data"]).decode("utf-8")
        assert decoded == "line1\nline2\n"

    def test_search_replace_missing_old_str_fails_clean(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rec = _ExecRecorder(stdout="abc")
        _attach(monkeypatch, rec)
        result = _run(
            fs_tools.file_write(
                "/f.txt",
                action="search_replace",
                old_str="zzz",
                new_str="y",
                _container_id="tb-x",
                _container_workdir="/",
            )
        )
        assert result.success is False
        assert "old_str not found" in str(result.error)


class TestOtherPrimitives:
    def test_create_directory_runs_mkdir(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rec = _ExecRecorder()
        _attach(monkeypatch, rec)
        result = _run(
            fs_tools.create_directory(
                "/app/logs/daily", _container_id="tb-x", _container_workdir="/app"
            )
        )
        assert result.success is True
        assert rec.calls[0]["args"] == ["/app/logs/daily"]
        assert "mkdir -p" in rec.calls[0]["script"]

    def test_copy_file_positional_args(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rec = _ExecRecorder()
        _attach(monkeypatch, rec)
        result = _run(
            fs_tools.copy_file(
                "/src/a.txt", "/dst/b.txt", _container_id="tb-x", _container_workdir="/"
            )
        )
        assert result.success is True
        call = rec.calls[-1]
        assert "cp -a" in call["script"]
        assert call["args"] == ["/src/a.txt", "/dst/b.txt"]

    def test_move_file(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rec = _ExecRecorder()
        _attach(monkeypatch, rec)
        result = _run(
            fs_tools.move_file(
                "/src/a.txt", "/dst/b.txt", _container_id="tb-x", _container_workdir="/"
            )
        )
        assert result.success is True
        assert "mv -f" in rec.calls[-1]["script"]

    def test_delete_file_recursive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rec = _ExecRecorder()
        _attach(monkeypatch, rec)
        result = _run(
            fs_tools.delete_file(
                "/tmp/dir", recursive=True, _container_id="tb-x", _container_workdir="/"
            )
        )
        assert result.success is True
        assert "rm -rf" in rec.calls[-1]["script"]


class TestNoBindingRegression:
    def test_file_read_without_container_uses_host_path(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "plain.txt"
        target.write_text("host-content", encoding="utf-8")
        calls: list[Any] = []

        async def _recorder(*a: Any, **k: Any) -> tuple[int, bytes, bytes]:
            calls.append((a, k))
            return 0, b"", b""

        _attach_raw(monkeypatch, _RawExecRecorder())
        monkeypatch.setattr(fs_tools, "_container_exec_raw", _recorder)
        result = _run(fs_tools.file_read(str(target), workspace=str(tmp_path)))
        assert result.success is True
        output = str(result.output)
        assert "host-content" in output
        assert calls == []  # host 路径零 docker 调用


class TestContainerExecStdin:
    def test_stdin_data_reaches_communicate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_container_exec 真身：stdin_data 经 communicate(input=...) 进容器侧
        stdin，且 argv 带 -i（docker exec 无 -i 不读 stdin——真内核探针实证：
        漏 -i / 漏 input 都会立即 EOF，base64 -d 写出空文件）。"""

        class _FakeProc:
            def __init__(self) -> None:
                self.communicate_inputs: list[bytes | None] = []
                self.returncode = 0

            async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
                self.communicate_inputs.append(input)
                return b"out", b""

        fake = _FakeProc()
        created: list[list[str]] = []

        async def _fake_create_subprocess_exec(*argv: Any, **kwargs: Any) -> _FakeProc:
            created.append(list(argv))
            assert kwargs.get("stdin") == fs_tools.asyncio.subprocess.PIPE
            return fake

        monkeypatch.setattr(
            fs_tools.asyncio, "create_subprocess_exec", _fake_create_subprocess_exec
        )
        rc, out, err = _run(
            fs_tools._container_exec(
                "tb-x",
                'base64 -d > "$1"',
                args=["/app/f.txt"],
                stdin_data='{"ok": 1}',
                timeout=5.0,
            )
        )
        assert rc == 0
        assert out == "out"
        assert err == ""
        assert created and created[0] == [
            "docker", "exec", "-i", "tb-x", "sh", "-c", 'base64 -d > "$1"', "sh", "/app/f.txt",
        ]
        assert fake.communicate_inputs == [b'{"ok": 1}']

    def test_no_stdin_uses_devnull_without_i_flag(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """无 stdin_data：stdin=DEVNULL，argv 无 -i，communicate(input=None)。"""

        class _FakeProc:
            def __init__(self) -> None:
                self.communicate_inputs: list[bytes | None] = []
                self.returncode = 0

            async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
                self.communicate_inputs.append(input)
                return b"", b""

        fake = _FakeProc()
        created: list[list[str]] = []

        async def _fake_create_subprocess_exec(*argv: Any, **kwargs: Any) -> _FakeProc:
            created.append(list(argv))
            assert kwargs.get("stdin") == fs_tools.asyncio.subprocess.DEVNULL
            return fake

        monkeypatch.setattr(
            fs_tools.asyncio, "create_subprocess_exec", _fake_create_subprocess_exec
        )
        rc, _out, _err = _run(fs_tools._container_exec("tb-x", 'cat "$1"', args=["/f"]))
        assert rc == 0
        assert fake.communicate_inputs == [None]
        assert created and "-i" not in created[0]


class TestContainerReadImageSplit:
    """R20 定稿：容器分支同走 _read_image_modality 装配单点（原图+分块一次注入）。

    双分支行为一致性由共享装配单点保证：本类只验容器面差异键（container）
    与无规格时不触达切分服务（向后兼容）。
    """

    _PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + b"payload-bytes"

    @pytest.fixture(autouse=True)
    def _reset_caller(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(fs_tools, "_split_image_caller", None)

    @staticmethod
    def _stub_tiled_caller(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, int]]:
        calls: list[tuple[str, str, int]] = []

        async def _caller(base64_data: str, mime_type: str, max_side: int) -> dict[str, Any]:
            calls.append((base64_data, mime_type, max_side))
            return {
                "tiled": True,
                "width": 2000,
                "height": 1000,
                "grid": {"rows": 1, "cols": 2},
                "tiles": [
                    {
                        "base64_data": "dGl6MA==",
                        "mime_type": "image/png",
                        "width": 1000,
                        "height": 1000,
                        "row": 1,
                        "col": 1,
                        "x0": 0,
                        "y0": 0,
                        "x1": 1000,
                        "y1": 1000,
                    },
                    {
                        "base64_data": "dGl6MQ==",
                        "mime_type": "image/png",
                        "width": 1000,
                        "height": 1000,
                        "row": 1,
                        "col": 2,
                        "x0": 1000,
                        "y0": 0,
                        "x1": 2000,
                        "y1": 1000,
                    },
                ],
                "annotation": "[图像切分] 原图 2000×1000 … 块(行1,列2) 覆盖 x=1000..2000",
            }

        fs_tools.set_split_image_caller(_caller)
        return calls

    def test_tiled_with_spec_keeps_container_key_and_tiles(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rec = _RawExecRecorder(stdout=self._PNG)
        _attach_raw(monkeypatch, rec)
        calls = self._stub_tiled_caller(monkeypatch)
        result = _run(
            fs_tools.file_read(
                "/app/data/poster.png",
                llm_vision_max_pixels=1048576,
                _container_id="tb-x",
                _container_workdir="/app",
            )
        )
        assert result.success is True
        out = result.output
        assert base64.b64decode(out["base64_data"]) == self._PNG
        assert out["container"] == "tb-x", "容器键保持（前端路由依赖）"
        assert [img["base64"] for img in out["images"]] == ["dGl6MA==", "dGl6MQ=="]
        assert out["tile_count"] == 2
        assert "块(行1,列2)" in out["content"]
        # 切分服务的入参 base64 = 容器读回的原始字节
        assert calls and calls[0][1] == "image/png" and calls[0][2] == 1048576

    def test_no_spec_keeps_r14_shape_without_split(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rec = _RawExecRecorder(stdout=self._PNG)
        _attach_raw(monkeypatch, rec)
        calls = self._stub_tiled_caller(monkeypatch)
        result = _run(
            fs_tools.file_read(
                "/app/data/probe.png", _container_id="tb-x", _container_workdir="/app"
            )
        )
        assert result.success is True
        assert set(result.output) == {"path", "mime_type", "base64_data", "size", "container"}
        assert calls == [], "无规格不得触达切分服务"
