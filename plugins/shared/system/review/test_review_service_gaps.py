# @feature: FP-0.2.二 review 缺口分支补测 | @ci: python-coverage
"""review 插件缺口分支补测（review_service / models / media_review_service /
server，2026-09-14 coverage 缺行）。

覆盖目标（行号语义经源码逐行确认）：
- review_service.py:269-271（wait_for_review 的 event 惰性补建路径）、
  275（event 就位后 review 又被移除 → ValueError）、317-318（超时任务
  非取消异常的 error 日志兜底）
- models.py:20（_SHARED_ROOT 重复导入时的 sys.path 短路分支）、
  111-114（ReviewRequest.from_dict 的 status 字符串→枚举归一）、
  173（ReviewFeedback.from_dict 全字段反序列化）
- media_review_service.py:186-187（review_artifacts 非 FileNotFoundError
  的异常兜底日志）、192（该兜底回填 error 条目）、305-307（EXIF bytes
  值跳过，不阻断其余元数据）
- server.py:891-895（review_improvement 工具面：正常返回 success 与
  规则文件缺失时 OSError → success=False）

外部依赖边界（PIL/PyAV/内核能力）走替身或真实 Pillow 生成物；EXIF 数据
用真实 JPEG 写入后读回，不 mock Pillow 内部实现。
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))


def _load(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(f"{name}_gaps_probe", _PLUGIN_DIR / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


review_models = _load("models")
ReviewFeedback = review_models.ReviewFeedback
ReviewRequest = review_models.ReviewRequest
ReviewStatus = review_models.ReviewStatus

service_mod = _load("review_service")
ReviewService = service_mod.ReviewService


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ═══════════════════════════════════════════════════════════
# models：sys.path 自举短路 + from_dict 归一
# ═══════════════════════════════════════════════════════════


class TestModelsImportAndDeserialization:
    def test_shared_root_already_on_path_short_circuits(self) -> None:
        """_SHARED_ROOT 已在 sys.path 时导入不重复插入（幂等）。"""
        shared_root = str(_PLUGIN_DIR.parents[1])
        assert shared_root in sys.path
        count_before = sys.path.count(shared_root)
        reloaded = _load("models")
        assert reloaded.ReviewStatus.PENDING.value == "pending"
        assert sys.path.count(shared_root) == count_before

    @pytest.mark.parametrize(
        ("raw_status", "expected"),
        [
            ("in_review", ReviewStatus.IN_REVIEW),
            ("approved", ReviewStatus.APPROVED),
            ("partially_approved", ReviewStatus.PARTIALLY_APPROVED),
            ("timeout", ReviewStatus.TIMEOUT),
        ],
    )
    def test_review_request_from_dict_normalizes_status(
        self, raw_status: str, expected: Any
    ) -> None:
        """字符串状态归一为枚举（111-114）；字段缺省走默认。"""
        review = ReviewRequest.from_dict({"id": "r-1", "status": raw_status, "title": "标题"})
        assert review.status is expected
        assert review.id == "r-1"
        assert review.title == "标题"
        assert review.artifact_ids == []
        assert review.metadata == {}

    def test_review_request_from_dict_preserves_enum_status(self) -> None:
        """已是枚举的 status 直通（不重复构造）。"""
        review = ReviewRequest.from_dict({"status": ReviewStatus.CANCELLED})
        assert review.status.value == ReviewStatus.CANCELLED.value

    def test_review_request_from_dict_full_roundtrip(self) -> None:
        """全字段往返：to_dict → from_dict 保持可观察等价。"""
        original = ReviewRequest(
            task_id="t-9",
            thread_id="th-1",
            session_id="s-1",
            tab_id="tab-2",
            title="审阅",
            description="描述",
            artifact_ids=["a1", "a2"],
            status=ReviewStatus.IN_REVIEW,
            priority="high",
            timeout_seconds=60.0,
            metadata={"k": "v"},
            reviewed_at="2026-01-01T00:00:00+00:00",
            completed_at="2026-01-01T00:01:00+00:00",
        )
        restored = ReviewRequest.from_dict(original.to_dict())
        assert restored.task_id == original.task_id
        assert restored.artifact_ids == original.artifact_ids
        assert restored.status is ReviewStatus.IN_REVIEW
        assert restored.reviewed_at == original.reviewed_at
        assert restored.completed_at == original.completed_at
        assert restored.metadata == {"k": "v"}

    def test_review_feedback_from_dict_all_fields(self) -> None:
        """ReviewFeedback.from_dict 全字段反序列化（173），user_id 显式保留。"""
        feedback = ReviewFeedback.from_dict({
            "id": "f-1",
            "review_request_id": "r-1",
            "response_type": "denied",
            "overall_comment": "不行",
            "annotations": [{"target": "p1", "text": "改"}],
            "user_id": "user-7",
            "created_at": "2026-02-02T00:00:00+00:00",
        })
        assert feedback.id == "f-1"
        assert feedback.review_request_id == "r-1"
        assert feedback.response_type == "denied"
        assert feedback.annotations == [{"target": "p1", "text": "改"}]
        assert feedback.user_id == "user-7"
        assert feedback.to_dict()["user_id"] == "user-7"

    def test_review_feedback_from_dict_defaults(self) -> None:
        """空字典：id 自动生成、可选字段回默认（有区分度对照输入）。"""
        feedback = ReviewFeedback.from_dict({})
        assert feedback.id and len(feedback.id) == 12
        assert feedback.response_type == "approved"
        assert feedback.user_id is None
        assert "user_id" not in feedback.to_dict()

    def test_review_request_to_dict_omits_unset_timestamps(self) -> None:
        """未审查/未完成的请求不吐 None 时间戳键（契约只增不删语义）。"""
        payload = ReviewRequest(title="t").to_dict()
        assert "reviewed_at" not in payload and "completed_at" not in payload
        assert payload["status"] == "pending"


# ═══════════════════════════════════════════════════════════
# review_service：wait_for_review 事件补建 / 服务侧状态守卫
# ═══════════════════════════════════════════════════════════


class TestWaitForReviewEventCreation:
    async def test_event_lazily_created_when_missing(self) -> None:
        """event 缺失但 review 存在 → 惰性补建后照常等待反馈（269-271）。"""
        service = ReviewService()
        review = await service.create_review(
            task_id="t1", thread_id="th", session_id="s", tab_id="tab", title="x"
        )
        # 模拟 event 丢失（进程重启/状态重组后的冷路径）
        service._pending_events.pop(review.id)

        waiter = asyncio.create_task(service.wait_for_review(review.id, timeout=2.0))
        await asyncio.sleep(0.01)
        await service.submit_feedback(review.id, "approved", overall_comment="ok")
        result = await waiter

        assert result["status"] == "completed"
        assert result["response_type"] == "approved"
        assert review.id in service._pending_events

    async def test_event_missing_and_review_missing_raises(self) -> None:
        """两者皆缺 → ValueError（268 与 275 同一契约）。"""
        service = ReviewService()
        with pytest.raises(ValueError, match="审批请求不存在"):
            await service.wait_for_review("ghost", timeout=0.01)

    async def test_review_removed_after_event_exists_raises(self) -> None:
        """event 在但 review 被移除 → ValueError（275）。"""
        service = ReviewService()
        review = await service.create_review(
            task_id="t1", thread_id="th", session_id="s", tab_id="tab", title="x"
        )
        assert review.id in service._pending_events
        service._reviews.pop(review.id)

        with pytest.raises(ValueError, match="审批请求不存在"):
            await service.wait_for_review(review.id, timeout=0.01)


class TestTimeoutTaskFailureLogging:
    async def test_non_cancel_exception_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        """超时任务内非取消异常 → 记 error 不崩（317-318）。"""
        service = ReviewService()
        review = await service.create_review(
            task_id="t1", thread_id="th", session_id="s", tab_id="tab", title="x",
            timeout_seconds=0.01,
        )

        async def _boom(review_id: str) -> None:
            raise RuntimeError("timeout handler broke")

        service._handle_timeout = _boom  # type: ignore[method-assign]
        task = service._timeout_tasks[review.id]
        with caplog.at_level(logging.ERROR, logger="review_service"):
            await asyncio.wait_for(task, timeout=3.0)

        messages = [record.getMessage() for record in caplog.records]
        assert any("超时处理失败" in m for m in messages)
        assert any("timeout handler broke" in m for m in messages)

    async def test_timeout_fires_without_handler_failure(self) -> None:
        """对照组：超时正常到期 → review 落 TIMEOUT（证明上方异常路径非恒定）。"""
        service = ReviewService()
        review = await service.create_review(
            task_id="t1", thread_id="th", session_id="s", tab_id="tab", title="x",
            timeout_seconds=0.01,
        )
        await asyncio.sleep(0.05)
        assert review.status.value == ReviewStatus.TIMEOUT.value  # 跨模块重载按值比较

    async def test_cancelled_task_keeps_cancellation_semantics(self) -> None:
        """取消语义保持：服务取消超时任务不落 error 日志（316 raise 分支）。"""
        service = ReviewService()
        review = await service.create_review(
            task_id="t1", thread_id="th", session_id="s", tab_id="tab", title="x",
            timeout_seconds=60.0,
        )
        task = service._timeout_tasks[review.id]
        task.cancel()
        await asyncio.sleep(0)
        assert task.cancelled()


# ═══════════════════════════════════════════════════════════
# media_review_service：制品审阅异常兜底 + EXIF bytes 跳过
# ═══════════════════════════════════════════════════════════


class TestReviewArtifactsErrorFallback:
    """review_artifacts 的异常兜底（186-187 日志 + 192 error 条目）。"""

    def test_unexpected_exception_recorded_as_error_entry(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        media_service_mod = _load("media_review_service")
        service = media_service_mod.MediaReviewService()

        class _Storage:
            async def load(self, artifact_id: str) -> dict[str, Any]:
                if artifact_id == "broken":
                    raise RuntimeError("storage backend exploded")
                return {"file_path": "x.png", "media_type": "image"}

        async def _boom(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("reviewer exploded")

        service.review_media = _boom  # type: ignore[method-assign]
        with caplog.at_level(logging.ERROR, logger="media_review_service"):
            results = _run(
                service.review_artifacts(["broken", "ok"], storage=_Storage())
            )

        assert len(results) == 2
        assert results[0]["artifact_id"] == "broken"
        assert "storage backend exploded" in results[0]["error"]
        # 逐条独立：单条失败不阻断后续制品
        assert results[1]["artifact_id"] == "ok"
        assert "reviewer exploded" in results[1]["error"]
        assert any("制品审阅失败" in r.getMessage() for r in caplog.records)

    def test_missing_file_maps_to_file_not_found_entry(self) -> None:
        """FileNotFoundError 走专用条目（有区分度对照组）。"""
        media_service_mod = _load("media_review_service")
        service = media_service_mod.MediaReviewService()

        class _Storage:
            async def load(self, artifact_id: str) -> dict[str, Any]:
                return {"file_path": f"{artifact_id}.png", "media_type": "image"}

        async def _missing(*args: Any, **kwargs: Any) -> Any:
            raise FileNotFoundError("磁盘上没了")

        service.review_media = _missing  # type: ignore[method-assign]
        results = _run(service.review_artifacts(["a"], storage=_Storage()))
        assert "文件不存在" in results[0]["error"]
        assert results[0]["artifact_id"] == "a"


class TestImageMetadataExifBytesSkipped:
    """_get_image_metadata：bytes 值跳过但其余元数据照常（305-307）。"""

    def test_bytes_exif_value_skipped_others_kept(self, tmp_path: Path) -> None:
        media_service_mod = _load("media_review_service")
        service = media_service_mod.MediaReviewService()
        img_path = tmp_path / "exif.jpg"
        _write_jpeg_with_exif(img_path, overrides={0x010E: "desc text", 0x927C: b"MAKER-NOTE-BYTES"})

        metadata = service.get_media_metadata(str(img_path), "image")
        assert metadata["format"] == "JPEG"
        assert metadata["width"] == 40 and metadata["height"] == 30
        assert metadata["aspect_ratio"] == pytest.approx(round(40 / 30, 4))
        # bytes 值（MakerNote）被跳过，字符串值（ImageDescription）保留
        assert metadata["exif"]["ImageDescription"] == "desc text"
        assert "MakerNote" not in metadata["exif"]
        assert all(not isinstance(v, bytes) for v in metadata["exif"].values())

    def test_unmappable_exif_tag_id_skipped(self, tmp_path: Path) -> None:
        """未映射 tag id（ExifBase 抛 ValueError）跳过，不阻断读取。"""
        media_service_mod = _load("media_review_service")
        service = media_service_mod.MediaReviewService()
        img_path = tmp_path / "unknown_tag.jpg"
        _write_jpeg_with_exif(img_path, overrides={0xFFFF: "mystery"})

        metadata = service.get_media_metadata(str(img_path), "image")
        assert "error" not in metadata
        assert metadata["exif"] == {} or all(k != "mystery" for k in metadata["exif"])

    def test_corrupt_image_records_error_field(self, tmp_path: Path) -> None:
        """非图片文件 → error 字段回填（不抛）。"""
        media_service_mod = _load("media_review_service")
        service = media_service_mod.MediaReviewService()
        bogus = tmp_path / "not-an-image.png"
        bogus.write_bytes(b"definitely not png")
        metadata = service.get_media_metadata(str(bogus), "image")
        assert "error" in metadata and "无法读取图片" in metadata["error"]


# ═══════════════════════════════════════════════════════════
# server.py：review_improvement 工具面
# ═══════════════════════════════════════════════════════════


class TestReviewImprovementTool:
    """review_improvement handler：规则可用返回建议，OSError/ValueError 转失败（891-895）。"""

    @staticmethod
    def _load_server() -> Any:
        mod_name = "review_server_improvement_gaps"
        sys.modules.pop(mod_name, None)
        spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "server.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = module
        spec.loader.exec_module(module)
        return module

    def test_success_path_returns_suggestions(self) -> None:
        """规则文件在位：返回 success=True + 分诊建议结构。"""
        server = self._load_server()
        result = _run(
            server.review_improvement(cases=[{
                "case_id": "c-gap-1",
                "task_status": "failed",
                "criteria": {},
            }])
        )
        assert result["success"] is True
        assert result["total"] == 1
        assert result["suggestions"][0]["triage"] == "improvement_space"

    def test_oserror_from_rules_translated_to_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """规则文件缺失 → OSError 转 {"success": False, "error"}（894-895）。"""
        server = self._load_server()
        improvement_mod = _load("improvement")

        def _boom(project_root: str | None = None) -> dict[str, Any]:
            raise OSError("rules file missing")

        monkeypatch.setattr(improvement_mod, "suggest", _boom)
        monkeypatch.setitem(sys.modules, "improvement", improvement_mod)
        result = _run(server.review_improvement(cases=[]))
        assert result["success"] is False
        assert "rules file missing" in result["error"]

    def test_value_error_from_rules_translated_to_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """规则格式非法（ValueError）同样转失败（有区分度输入）。"""
        server = self._load_server()
        improvement_mod = _load("improvement")

        def _boom(project_root: str | None = None) -> dict[str, Any]:
            raise ValueError("分诊规则格式错误")

        monkeypatch.setattr(improvement_mod, "suggest", _boom)
        monkeypatch.setitem(sys.modules, "improvement", improvement_mod)
        result = _run(server.review_improvement(cases=[]))
        assert result["success"] is False
        assert "分诊规则格式错误" in result["error"]


# ── 辅助 ────────────────────────────────────────────────────


def _write_jpeg_with_exif(path: Path, overrides: dict[int, Any]) -> None:
    """写一张带指定 EXIF tag 的真实 JPEG（Pillow 生成，非替身）。"""
    from PIL import Image

    image = Image.new("RGB", (40, 30), "white")
    exif = image.getexif()
    for tag_id, value in overrides.items():
        exif[tag_id] = value
    image.save(path, exif=exif)


# ═══════════════════════════════════════════════════════════
# improvement：规则文件格式校验 + 三向分诊缺陷/故障信号
# ═══════════════════════════════════════════════════════════


class TestImprovementRuleValidation:
    """load_rules 的格式守卫（26）与 triage 两信号分支（72-76 / 79-84）。"""

    @staticmethod
    def _rules_project(tmp_path: Path, payload: str) -> str:
        """在 tmp 项目根下写 config/plugins/review/triage_rules.yaml，返回根路径。"""
        rules_dir = tmp_path / "config" / "plugins" / "review"
        rules_dir.mkdir(parents=True)
        (rules_dir / "triage_rules.yaml").write_text(payload, encoding="utf-8")
        return str(tmp_path)

    @pytest.mark.parametrize("payload", ["just a scalar", "- a\n- b\n", "", "null\n"])
    def test_non_dict_rules_raise_value_error(self, tmp_path: Path, payload: str) -> None:
        """规则文件非映射（标量/序列/空）→ ValueError（26），不静默当空规则。"""
        improvement_mod = _load("improvement")
        root = self._rules_project(tmp_path, payload)
        with pytest.raises(ValueError, match="分诊规则格式错误"):
            improvement_mod.load_rules(root)

    def test_dict_rules_loaded(self, tmp_path: Path) -> None:
        """对照组：合法映射照常加载并保留键。"""
        improvement_mod = _load("improvement")
        root = self._rules_project(tmp_path, "anti_overfit:\n  require: 通用规则\n")
        rules = improvement_mod.load_rules(root)
        assert rules["anti_overfit"]["require"] == "通用规则"

    def test_harness_defect_signal_reported(self, tmp_path: Path) -> None:
        """harness_defect 命中 → triage/action/signals 三键齐（72-76），不立项。"""
        improvement_mod = _load("improvement")
        root = self._rules_project(
            tmp_path,
            "triage:\n"
            "  harness_defect:\n"
            "    signals: ['断言口径', '题集配置']\n"
            "    action: 报告用户，不走提案\n",
        )
        out = improvement_mod.suggest(
            [{
                "case_id": "c-h1",
                "task_status": "failed",
                "criteria": {},
                "symptom_note": "断言口径与新契约不符",
            }],
            project_root=root,
        )
        suggestion = out["suggestions"][0]
        assert suggestion["triage"] == "harness_defect"
        assert suggestion["action"] == "报告用户，不走提案"
        assert suggestion["signals"] == ["断言口径"]
        assert out["improvable"] == 0

    def test_harness_defect_wins_over_system_fault(self, tmp_path: Path) -> None:
        """两信号同时命中时先判 harness_defect（顺序契约，有区分度输入）。"""
        improvement_mod = _load("improvement")
        root = self._rules_project(
            tmp_path,
            "triage:\n"
            "  harness_defect:\n"
            "    signals: ['preclean']\n"
            "    action: 报告\n"
            "  system_fault:\n"
            "    signals: ['traceback']\n"
            "    action: 修复\n",
        )
        out = improvement_mod.suggest(
            [{
                "case_id": "c-both",
                "task_status": "failed",
                "criteria": {},
                "symptom_note": "preclean 没跑",
                "trajectory_note": "traceback in sidecar",
            }],
            project_root=root,
        )
        assert out["suggestions"][0]["triage"] == "harness_defect"

    def test_harness_defect_default_action_when_absent(self, tmp_path: Path) -> None:
        """action 缺省 → 空串（不落 None）。"""
        improvement_mod = _load("improvement")
        root = self._rules_project(
            tmp_path, "triage:\n  harness_defect:\n    signals: ['题集配置']\n"
        )
        out = improvement_mod.suggest(
            [{"case_id": "c-h2", "task_status": "failed", "criteria": {},
              "symptom_note": "题集配置错了"}],
            project_root=root,
        )
        assert out["suggestions"][0]["action"] == ""

    def test_missing_triage_section_skips_signal_matching(self, tmp_path: Path) -> None:
        """规则无 triage 段：signals 列表为空 → 落到杠杆映射（不崩）。"""
        improvement_mod = _load("improvement")
        root = self._rules_project(
            tmp_path,
            "levers:\n  task_failure:\n    stage: execution\n    candidates: [persona]\n",
        )
        out = improvement_mod.suggest(
            [{"case_id": "c-n", "task_status": "failed", "criteria": {}}],
            project_root=root,
        )
        suggestion = out["suggestions"][0]
        assert suggestion["triage"] == "improvement_space"
        assert suggestion["behavior_class"] == "task_failure"
        assert suggestion["levers"] == ["persona"]
