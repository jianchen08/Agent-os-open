# @feature: FP-0.2.二 review 缺口分支补测 | @ci: python-coverage
"""review 插件分支缺口补测（media_reviewer + server，2026-09-13 coverage 缺行）。

media_reviewer（媒体审阅确定性规则校验，无 LLM）：
- 可选依赖（PyAV/PIL）任一缺失时模块导入不崩、置 None 降级
- 图片尺寸超限逐项报错；EXIF 分数值安全转换（含退化输入）与 GPS IFD 提取
- 视频格式无法识别/不支持报错；时长来源（容器优先、流回退）；
  元数据提取异常降级 invalid；关键帧抽帧间隔单调性与失败上抛+句柄必关

server（复盘 MCP 面与 HTTP 面）：
- 报告落 Hindsight 失败不崩回写（内存照常 completed）
- chat 能力缺席/为 None → local_degrade；复盘管道登记失败不影响派发
- pipeline-state 轮询失败/行缺失保持 running（可重复轮询）
- 冷读对畸形 documents/search 行逐条跳过，精确匹配才采纳
- _on_load 注入记忆后端后冷读生效；媒体审阅上传面协议级错误 400

外部依赖边界（PyAV/PIL/IMemoryBackend/内核能力）一律替身注入，不接真实后端。
"""

from __future__ import annotations

import base64
import importlib.util
import json
import sys
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from agentos_plugin_sdk.capability import CapabilityHandle

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

import media_reviewer  # noqa: E402
from media_reviewer import ImageReviewer, VideoReviewer  # noqa: E402
from models import MediaReviewConfig  # noqa: E402

from PIL import Image as PILImage  # noqa: E402
from PIL.ExifTags import Base as ExifBase  # noqa: E402
from PIL.ExifTags import GPSTAGS  # noqa: E402


# ═══════════════════════════════════════════════════════════
# media_reviewer：PIL 边界替身（EXIF 数据源可编程，格式/尺寸走真实验证）
# ═══════════════════════════════════════════════════════════


class _FakeExifSource:
    """getexif() 返回替身：.get(tag)/.get_ifd(tag) 可编程。"""

    def __init__(self, tags: dict[int, Any], gps: dict[int, Any] | None = None) -> None:
        self._tags = tags
        self._gps = gps or {}

    def get(self, tag_id: int, default: Any = None) -> Any:
        return self._tags.get(tag_id, default)

    def get_ifd(self, tag_id: int) -> dict[int, Any]:
        return self._gps

    def __bool__(self) -> bool:
        return bool(self._tags) or bool(self._gps)


class _FakePILImage:
    """PIL Image 替身：合法尺寸+格式，EXIF 来自注入数据。"""

    def __init__(self, tags: dict[int, Any], gps: dict[int, Any] | None = None) -> None:
        self.format = "PNG"
        self.size = (100, 50)
        self._exif = _FakeExifSource(tags, gps)

    def load(self) -> None:
        return None

    def getexif(self) -> _FakeExifSource:
        return self._exif


def _install_fake_pil(monkeypatch: pytest.MonkeyPatch, img: _FakePILImage) -> None:
    monkeypatch.setattr(media_reviewer, "Image", SimpleNamespace(open=lambda path: img))


class TestOptionalDependencyFallback:
    """重依赖缺失契约：模块导入不崩，缺的依赖置 None（审阅时降级）。"""

    @pytest.mark.parametrize(
        "blocked_module,attr_name",
        [
            pytest.param("av", "av", id="pyav_missing"),
            pytest.param("PIL", "Image", id="pillow_missing"),
        ],
    )
    def test_missing_optional_dependency_imports_as_none(
        self, monkeypatch: pytest.MonkeyPatch, blocked_module: str, attr_name: str
    ) -> None:
        monkeypatch.setitem(sys.modules, blocked_module, None)
        spec = importlib.util.spec_from_file_location(
            "media_reviewer_gap_probe", _PLUGIN_DIR / "media_reviewer.py"
        )
        assert spec is not None and spec.loader is not None
        fresh = importlib.util.module_from_spec(spec)
        sys.modules["media_reviewer_gap_probe"] = fresh
        spec.loader.exec_module(fresh)  # 导入本身不崩即为契约
        assert getattr(fresh, attr_name) is None


class TestImageDimensionLimits:
    """图片尺寸检查：超上限逐维报错（真 PNG 落盘，真 Pillow 解析）。"""

    @pytest.mark.parametrize(
        "config_kwargs,expected_error_count",
        [
            pytest.param({"image_max_width": 50}, 1, id="width_over_max_only"),
            pytest.param(
                {"image_max_width": 50, "image_max_height": 10},
                2,
                id="width_and_height_over_max",
            ),
        ],
    )
    def test_oversize_image_reports_dimension_errors(
        self, tmp_path: Path, config_kwargs: dict[str, int], expected_error_count: int
    ) -> None:
        media = tmp_path / "big.png"
        PILImage.new("RGB", (100, 50)).save(media)

        result = ImageReviewer.review(str(media), MediaReviewConfig(**config_kwargs))

        assert result.is_valid is False
        assert len(result.errors) == expected_error_count  # 上限越紧报错越多（单调）
        assert any(f"图片宽度 {100}px 超过最大限制" in e for e in result.errors)
        assert (result.width, result.height) == (100, 50)


class TestImageExifExtraction:
    """EXIF 提取：分数 tuple 安全转换（含退化输入）与 GPS IFD 映射。"""

    @pytest.mark.parametrize(
        "raw_value",
        [
            pytest.param((35, 2), id="rational_tuple"),
            pytest.param((1, 0), id="zero_denominator"),
            pytest.param((8,), id="single_element_tuple"),
        ],
    )
    def test_exif_tuple_value_safe_conversion(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        raw_value: tuple[int, ...],
    ) -> None:
        media = tmp_path / "exif.png"
        media.write_bytes(b"stub-image-bytes")  # Image.open 已替身，内容不参与
        _install_fake_pil(monkeypatch, _FakePILImage({ExifBase.FocalLength: raw_value}))

        result = ImageReviewer.review(str(media))

        assert result.is_valid is True
        got = result.exif["FocalLength"]
        if len(raw_value) == 2 and raw_value[1] != 0:
            assert got == raw_value[0] / raw_value[1]  # 可转换分数 → float
            assert isinstance(got, float)
        else:
            assert got == str(raw_value)  # 退化输入 → 原样字符串，不崩

    def test_exif_gps_ifd_extracted_with_tag_name_mapping(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        media = tmp_path / "gps.png"
        media.write_bytes(b"stub-image-bytes")
        gps = {
            1: "N",  # GPSTAGS[1] = "GPSLatitudeRef"
            2: ((37, 1), (24, 1), (30, 1)),  # GPSTAGS[2] = "GPSLatitude"
            9999: "mystery",
        }
        _install_fake_pil(monkeypatch, _FakePILImage({ExifBase.Make: "Cam"}, gps))

        result = ImageReviewer.review(str(media))

        assert result.exif["Make"] == "Cam"
        assert result.exif[f"GPS_{GPSTAGS[1]}"] == "N"
        # GPS 值原样保留（不走 tuple 分数转换）
        assert result.exif[f"GPS_{GPSTAGS[2]}"] == ((37, 1), (24, 1), (30, 1))
        # 未知 GPS tag 兜底命名 GPS_<id>
        assert result.exif["GPS_GPS_9999"] == "mystery"


class TestMissingFileContract:
    """三类审阅入口对不存在文件统一抛 FileNotFoundError。"""

    @pytest.mark.parametrize(
        "review_call",
        [
            pytest.param(lambda p: ImageReviewer.review(p), id="image_review"),
            pytest.param(lambda p: VideoReviewer.review(p), id="video_review"),
            pytest.param(lambda p: VideoReviewer.extract_keyframes(p), id="keyframes"),
        ],
    )
    def test_missing_file_raises_file_not_found(
        self, tmp_path: Path, review_call: Any
    ) -> None:
        with pytest.raises(FileNotFoundError, match="文件不存在"):
            review_call(str(tmp_path / "ghost.png"))


# ═══════════════════════════════════════════════════════════
# media_reviewer：PyAV 边界替身（容器/流可编程；帧图用真 PIL 落盘）
# ═══════════════════════════════════════════════════════════


class _FakeVideoStream:
    def __init__(
        self,
        *,
        time_base: Fraction = Fraction(1, 1000),
        average_rate: float = 24.0,
        duration: int | None = None,
        width: int = 640,
        height: int = 360,
        codec: str = "h264",
    ) -> None:
        self.time_base = time_base
        self.average_rate = average_rate
        self.duration = duration
        self.width = width
        self.height = height
        self.codec_context = SimpleNamespace(name=codec)


class _FakeFrame:
    def __init__(self, pts: int, size: tuple[int, int] = (4, 4)) -> None:
        self.pts = pts
        self._size = size

    def to_image(self) -> PILImage.Image:
        return PILImage.new("RGB", self._size)


class _FakeContainer:
    def __init__(
        self,
        stream: _FakeVideoStream,
        *,
        format_name: str = "mp4",
        duration: int | None = None,
        frames: list[_FakeFrame] | None = None,
        decode_error: Exception | None = None,
        format_error: Exception | None = None,
    ) -> None:
        self.streams = SimpleNamespace(video=[stream])
        self._format_name = format_name
        self._duration = duration
        self._frames = frames or []
        self._decode_error = decode_error
        self._format_error = format_error
        self.closed = False

    @property
    def format(self) -> SimpleNamespace:
        if self._format_error is not None:
            raise self._format_error
        return SimpleNamespace(name=self._format_name)

    @property
    def duration(self) -> int | None:
        return self._duration

    def seek(self, pts: int) -> None:
        return None

    def decode(self, video: int = 0) -> Any:
        if self._decode_error is not None:
            raise self._decode_error
        return iter(self._frames)

    def close(self) -> None:
        self.closed = True


class _FakeAv:
    time_base = 1_000_000

    def __init__(self, target: Any) -> None:
        self._target = target

    def open(self, file_path: str) -> Any:
        if isinstance(self._target, Exception):
            raise self._target
        return self._target


def _install_fake_av(monkeypatch: pytest.MonkeyPatch, target: Any) -> None:
    monkeypatch.setattr(media_reviewer, "av", _FakeAv(target))


class TestExtractKeyframes:
    """关键帧抽帧：默认输出目录、间隔单调性、失败上抛且容器必关。"""

    @pytest.mark.parametrize(
        "interval,expected_count",
        [
            pytest.param(5.0, 2, id="interval_5s"),
            pytest.param(3.0, 3, id="interval_3s"),
        ],
    )
    def test_keyframes_default_output_dir_and_interval_monotonicity(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        interval: float,
        expected_count: int,
    ) -> None:
        video = tmp_path / "clip.mp4"
        video.write_bytes(b"not-a-real-video")  # PyAV 为替身，内容不参与解析
        frames = [_FakeFrame(0), _FakeFrame(3000), _FakeFrame(5000), _FakeFrame(8000)]
        container = _FakeContainer(_FakeVideoStream(time_base=Fraction(1, 1000)), frames=frames)
        _install_fake_av(monkeypatch, container)

        paths = VideoReviewer.extract_keyframes(str(video), interval_seconds=interval)

        assert len(paths) == expected_count  # 间隔越大抽帧越少（单调）
        assert all(Path(p).parent == tmp_path for p in paths)  # output_dir=None → 视频同目录
        assert all(Path(p).suffix == ".jpg" and Path(p).is_file() for p in paths)
        assert container.closed is True

    @pytest.mark.parametrize(
        "failure,where",
        [
            pytest.param(RuntimeError("decoder unavailable"), "open", id="open_failure"),
            pytest.param(RuntimeError("decode exploded"), "decode", id="decode_failure"),
        ],
    )
    def test_keyframes_failure_propagates_and_closes_container(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        failure: Exception,
        where: str,
    ) -> None:
        video = tmp_path / "broken.mp4"
        video.write_bytes(b"x")
        container: _FakeContainer | None = None
        if where == "open":
            _install_fake_av(monkeypatch, failure)
        else:
            container = _FakeContainer(_FakeVideoStream(), decode_error=failure)
            _install_fake_av(monkeypatch, container)

        with pytest.raises(RuntimeError, match="unavailable|exploded"):
            VideoReviewer.extract_keyframes(str(video))

        if container is not None:
            assert container.closed is True  # 解码中途抛错句柄仍必关


class TestVideoReviewMetadata:
    """视频元数据：格式判定、时长来源与提取异常降级。"""

    @pytest.mark.parametrize(
        "ext,format_name,expected_error",
        [
            pytest.param(".bin", "", "无法识别视频格式", id="unidentifiable_format"),
            pytest.param(".rec", "weird", "不支持的视频格式", id="unsupported_format"),
        ],
    )
    def test_format_validation_errors(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        ext: str,
        format_name: str,
        expected_error: str,
    ) -> None:
        media = tmp_path / f"clip{ext}"
        media.write_bytes(b"v")
        container = _FakeContainer(
            _FakeVideoStream(), format_name=format_name, duration=2_000_000
        )
        _install_fake_av(monkeypatch, container)

        result = VideoReviewer.review(str(media))

        assert result.is_valid is False
        assert len(result.errors) == 1
        assert result.errors[0].startswith(expected_error)

    @pytest.mark.parametrize(
        "container_duration,expected_duration",
        [
            pytest.param(5_000_000, 5.0, id="container_duration_priority"),
            pytest.param(None, 3.0, id="stream_duration_fallback"),
        ],
    )
    def test_duration_source_priority(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        container_duration: int | None,
        expected_duration: float,
    ) -> None:
        media = tmp_path / "clip.mp4"
        media.write_bytes(b"v")
        stream = _FakeVideoStream(
            time_base=Fraction(1, 2000), duration=6000  # 6000 * (1/2000) = 3s
        )
        container = _FakeContainer(stream, format_name="mp4", duration=container_duration)
        _install_fake_av(monkeypatch, container)

        result = VideoReviewer.review(str(media))

        assert result.is_valid is True
        assert result.duration_seconds == expected_duration
        assert result.format == "MP4"  # 扩展名优先于容器格式列表
        assert result.codec == "h264"
        assert (result.width, result.height) == (640, 360)
        assert 0 < result.fps <= 120  # fps 落在合理区间（性质断言）

    def test_metadata_extraction_failure_degrades_to_invalid(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        media = tmp_path / "clip.mp4"
        media.write_bytes(b"v")
        container = _FakeContainer(
            _FakeVideoStream(), format_error=AttributeError("format exploded")
        )
        _install_fake_av(monkeypatch, container)

        result = VideoReviewer.review(str(media))

        assert result.is_valid is False
        assert result.errors == ["无法解析视频文件"]
        assert container.closed is True  # 提取异常句柄仍必关


# ═══════════════════════════════════════════════════════════
# server：复盘派发/轮询/冷读/HTTP 上传面分支
# ═══════════════════════════════════════════════════════════


def _load_server_module() -> Any:
    """每次新建 server 模块（隔离 _reports/_memory_backend 全局态）。"""
    spec = importlib.util.spec_from_file_location(
        "review_server_gaps_test", _PLUGIN_DIR / "server.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["review_server_gaps_test"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def mod() -> Any:
    module = _load_server_module()
    module._reports.clear()
    module._memory_backend = None
    return module


class _StubMemoryBackend:
    """IMemoryBackend 替身：add 可注错，documents/search 行可编程。"""

    def __init__(self) -> None:
        self.documents: list[Any] = []
        self.search_rows: list[Any] = []
        self.add_error: Exception | None = None

    async def add(
        self,
        user_id: str,
        content: str,
        memory_type: str = "semantic",
        tags: list[str] | None = None,
        source: str = "",
        metadata: dict[str, str] | None = None,
    ) -> str:
        if self.add_error is not None:
            raise self.add_error
        return "mem-1"

    async def get_documents(
        self,
        user_id: str,
        tags: list[str] | None = None,
        tags_match: str = "any_strict",
        limit: int = 20,
    ) -> list[Any]:
        return list(self.documents[:limit])

    async def search(
        self,
        query: str,
        user_id: str,
        top_k: int = 5,
        memory_type: str | None = None,
    ) -> list[Any]:
        return list(self.search_rows[:top_k])


class _SearchOnlyBackend(_StubMemoryBackend):
    """旧形态后端：无 documents 面，冷读走相似度 search 回落。"""

    get_documents = None  # type: ignore[assignment]


def _inject_chat(
    mod: Any,
    *,
    pipeline_id: str = "pipe_gaps_1",
    fail_registration: bool = False,
) -> None:
    """注入 fake chat 能力；fail_registration 使登记调用（task.owned.*）抛错。"""

    async def call_fn(method: str, params: dict[str, Any], timeout: float | None = None) -> Any:
        if fail_registration and any(
            str(k).startswith("task.owned.") for k in (params.get("state") or {})
        ):
            raise RuntimeError("registration channel down")
        return {"pipeline_id": pipeline_id}

    mod.plugin._capabilities["chat"] = CapabilityHandle("chat", call_fn=call_fn)


class TestStoreReportBackendFailure:
    """报告落 Hindsight 失败不崩回写：内存仍 completed（get_report 可即时轮询）。"""

    @pytest.mark.parametrize(
        "failure",
        [
            pytest.param(RuntimeError("hindsight down"), id="runtime_error"),
            pytest.param(TimeoutError("flush timeout"), id="timeout"),
        ],
    )
    async def test_store_report_survives_backend_failure(
        self, mod: Any, failure: Exception
    ) -> None:
        stub = _StubMemoryBackend()
        stub.add_error = failure
        mod.set_memory_backend(stub)

        await mod.store_report("review-f1", {"task_id": "task-f1", "lessons": ["l-f1"]})

        entry = mod._reports["review-f1"]
        assert entry["status"] == "completed"
        assert entry["lessons"] == ["l-f1"]
        assert entry["review_id"] == "review-f1"


class TestTriggerReviewDegradations:
    """chat 能力缺席/为 None → local_degrade；登记失败不影响派发。"""

    @pytest.mark.parametrize(
        "chat_mode",
        [pytest.param("absent", id="capability_absent"), pytest.param("none", id="capability_none")],
    )
    async def test_chat_unavailable_degrades(self, mod: Any, chat_mode: str) -> None:
        if chat_mode == "absent":
            mod.plugin._capabilities.pop("chat", None)
        else:
            mod.plugin._capabilities["chat"] = None

        r = await mod.trigger_review(task_id="task-g1", summary="s")

        assert r["status"] == "degraded"
        assert r["mode"] == "local_degrade"
        assert mod._reports[r["review_id"]]["status"] == "degraded"

    async def test_registration_failure_does_not_break_dispatch(self, mod: Any) -> None:
        """复盘管道登记到任务管道失败 → 仅告警，派发与报告登记照常。"""
        _inject_chat(mod, pipeline_id="pipe_reg_fail", fail_registration=True)

        r = await mod.trigger_review(
            task_id="task-g2", summary="复盘对象", metrics={"quality": 0.9}
        )

        assert r["status"] == "running"
        assert r["pipeline_id"] == "pipe_reg_fail"
        report = mod._reports[r["review_id"]]
        assert report["status"] == "running"
        assert report["pipeline_id"] == "pipe_reg_fail"


class TestPipelinePollResilience:
    """轮询面：能力缺失/调用失败/行缺失均保持 running 且可重复轮询。"""

    async def _trigger_running(self, mod: Any) -> str:
        _inject_chat(mod)
        r = await mod.trigger_review(task_id="task-g3", summary="s")
        return r["review_id"]

    async def test_pipeline_state_capability_absent_keeps_running(self, mod: Any) -> None:
        rid = await self._trigger_running(mod)
        mod.plugin._capabilities.pop("pipeline-state", None)

        report = await mod.get_report(rid)

        assert report["status"] == "running"
        assert (await mod.get_report(rid))["status"] == "running"  # 幂等轮询

    async def test_pipeline_state_call_error_keeps_running(self, mod: Any) -> None:
        rid = await self._trigger_running(mod)

        async def call_fn(method: str, params: dict[str, Any], timeout: float | None = None) -> Any:
            raise RuntimeError("pipeline-state down")

        mod.plugin._capabilities["pipeline-state"] = CapabilityHandle(
            "pipeline-state", call_fn=call_fn
        )

        report = await mod.get_report(rid)

        assert report["status"] == "running"

    async def test_pipeline_row_missing_keeps_running(self, mod: Any) -> None:
        rid = await self._trigger_running(mod)

        async def call_fn(method: str, params: dict[str, Any], timeout: float | None = None) -> Any:
            return [{"pipeline_id": "other_pipe", "status": "completed"}, "junk-row"]

        mod.plugin._capabilities["pipeline-state"] = CapabilityHandle(
            "pipeline-state", call_fn=call_fn
        )

        report = await mod.get_report(rid)

        assert report["status"] == "running"
        assert report["pipeline_id"] == "pipe_gaps_1"


class TestColdReadMalformedRows:
    """冷读：畸形 documents/search 行逐条跳过，精确匹配 review_id 才采纳。"""

    async def test_search_fallback_skips_malformed_rows(self, mod: Any) -> None:
        stub = _SearchOnlyBackend()
        stub.search_rows = [
            "not-a-dict",
            123,
            {"content": 999},
            {"content": json.dumps({"review_id": "review-other", "status": "completed"})},
            {
                "content": json.dumps(
                    {"review_id": "review-s1", "status": "completed", "lessons": ["l-s1"]}
                )
            },
        ]
        mod.set_memory_backend(stub)

        got = await mod.get_report("review-s1")

        assert got.get("error") is None
        assert got["review_id"] == "review-s1"
        assert got["lessons"] == ["l-s1"]
        assert mod._reports["review-s1"]["status"] == "completed"  # 命中后回填内存

    async def test_documents_path_skips_malformed_docs(self, mod: Any) -> None:
        stub = _StubMemoryBackend()
        stub.documents = [
            "junk",
            {"original_text": 42},
            {"original_text": "{not-json"},
            {"original_text": json.dumps({"review_id": "review-other"})},
            {
                "original_text": json.dumps(
                    {"review_id": "review-d1", "status": "completed", "task_id": "task-d1"}
                )
            },
        ]
        mod.set_memory_backend(stub)

        got = await mod.get_report("review-d1")

        assert got.get("error") is None
        assert got["task_id"] == "task-d1"
        assert mod._reports["review-d1"]["status"] == "completed"


class TestOnLoadBackendInjection:
    """_on_load：后端注入后冷读生效；构建失败（None）保持仅内存降级。"""

    async def test_on_load_injects_backend_and_enables_cold_read(
        self, mod: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stub = _StubMemoryBackend()
        stub.documents = [
            {
                "original_text": json.dumps(
                    {
                        "review_id": "review-load1",
                        "status": "completed",
                        "lessons": ["l-load"],
                    }
                )
            }
        ]
        monkeypatch.setattr(mod, "build_memory_backend", lambda plugin: stub)

        await mod._on_load({})

        got = await mod.get_report("review-load1")
        assert got.get("error") is None
        assert got["lessons"] == ["l-load"]

    async def test_on_load_without_backend_stays_memory_only(
        self, mod: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(mod, "build_memory_backend", lambda plugin: None)

        await mod._on_load({})

        got = await mod.get_report("review-none-load")
        assert got == {"error": "review not found", "review_id": "review-none-load"}


class TestMediaReviewUploadProtocolErrors:
    """媒体审阅上传面协议级错误：success 信封内携 HTTP 400 + 结构化错误体。"""

    @pytest.mark.parametrize(
        "case,expected_message_prefix",
        [
            pytest.param("bad_base64", "invalid upload body", id="bad_base64"),
            pytest.param("bad_multipart", "multipart parse failed", id="malformed_multipart"),
        ],
    )
    async def test_upload_protocol_errors_return_400(
        self, mod: Any, case: str, expected_message_prefix: str
    ) -> None:
        if case == "bad_base64":
            raw_body = "!!!definitely-not-base64!!!"
            headers = {"content-type": "multipart/form-data; boundary=b"}
        else:
            raw_body = base64.b64encode(b"--b\r\n").decode("ascii")
            # 非法边界字符（lone surrogate）→ multipart 解析即抛
            headers = {"content-type": "multipart/form-data; boundary=bad\udcff"}

        resp = await mod.http_handle(
            path="/ext/review_service/reviews/media-review",
            method="POST",
            raw_body=raw_body,
            headers=headers,
        )

        assert resp["success"] is True  # 协议级错误仍是 success:true 信封
        data = resp["data"]
        assert data["status"] == 400
        body = json.loads(base64.b64decode(data["body"]))
        assert body["error"]["code"] == "400"
        assert body["error"]["message"].startswith(expected_message_prefix)
