"""ComfyUI 模块单元测试。

测试覆盖：
- services/comfyui_history.py: GenerationRecord、GenerationHistory
- services/comfyui_service.py: ComfyUIService（Mock 连接器）
- channels/api/routes_comfyui.py: API 路由（httpx AsyncClient）
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ============================================================
# Fixtures
# ============================================================


@pytest.fixture
def tmp_history_file(tmp_path: Path) -> Path:
    """创建临时历史文件路径。"""
    return tmp_path / "history.json"


@pytest.fixture
def tmp_workflow_dir(tmp_path: Path) -> Path:
    """创建临时工作流模板目录。"""
    wf_dir = tmp_path / "workflows"
    wf_dir.mkdir()
    template = {
        "3": {
            "class_type": "KSampler",
            "inputs": {
                "prompt": "{{prompt}}",
                "negative_prompt": "{{negative_prompt}}",
                "width": "{{width}}",
                "height": "{{height}}",
                "steps": "{{steps}}",
                "cfg": "{{cfg_scale}}",
                "seed": "{{seed}}",
            },
        }
    }
    (wf_dir / "default_txt2img.json").write_text(
        json.dumps(template), encoding="utf-8"
    )
    return wf_dir


@pytest.fixture
def history(tmp_history_file: Path) -> "GenerationHistory":
    """创建 GenerationHistory 实例。"""
    from services.comfyui_history import GenerationHistory
    return GenerationHistory(str(tmp_history_file))


# ============================================================
# GenerationRecord 测试
# ============================================================


class TestGenerationRecord:
    """GenerationRecord 数据模型测试。"""

    def test_create_record_with_defaults(self) -> None:
        """默认值创建记录。"""
        from services.comfyui_history import GenerationRecord, GenerationStatus
        record = GenerationRecord(
            prompt="a cat",
            negative_prompt="blurry",
            template_name="default_txt2img",
        )
        assert record.prompt == "a cat"
        assert record.negative_prompt == "blurry"
        assert record.template_name == "default_txt2img"
        assert record.status == GenerationStatus.PENDING
        assert record.progress == 0
        assert record.result_images == []
        assert record.error is None
        assert record.id

    def test_create_record_with_custom_status(self) -> None:
        """指定状态创建记录。"""
        from services.comfyui_history import GenerationRecord, GenerationStatus
        record = GenerationRecord(
            prompt="test",
            negative_prompt="",
            template_name="t",
            status=GenerationStatus.RUNNING,
            progress=50,
        )
        assert record.status == GenerationStatus.RUNNING
        assert record.progress == 50

    def test_to_dict(self) -> None:
        """序列化为字典。"""
        from services.comfyui_history import GenerationRecord, GenerationStatus
        record = GenerationRecord(
            prompt="a cat",
            negative_prompt="blurry",
            template_name="default_txt2img",
            status=GenerationStatus.COMPLETED,
            progress=100,
            result_images=["/output/img1.png"],
        )
        d = record.to_dict()
        assert d["prompt"] == "a cat"
        assert d["status"] == "completed"
        assert d["progress"] == 100
        assert d["result_images"] == ["/output/img1.png"]
        assert "id" in d
        assert "created_at" in d

    def test_from_dict_roundtrip(self) -> None:
        """序列化后反序列化应保持一致。"""
        from services.comfyui_history import GenerationRecord
        original = GenerationRecord(
            prompt="hello",
            negative_prompt="bad",
            template_name="tpl",
            progress=30,
            result_images=["a.png"],
        )
        d = original.to_dict()
        restored = GenerationRecord.from_dict(d)
        assert restored.prompt == original.prompt
        assert restored.id == original.id
        assert restored.progress == original.progress
        assert restored.result_images == original.result_images


# ============================================================
# GenerationHistory 测试
# ============================================================


class TestGenerationHistory:
    """GenerationHistory 历史管理测试。"""

    def test_add_and_get(self, history: "GenerationHistory") -> None:
        """添加记录后可按 ID 获取。"""
        from services.comfyui_history import GenerationRecord
        record = GenerationRecord(
            prompt="test", negative_prompt="", template_name="t"
        )
        history.add(record)
        got = history.get(record.id)
        assert got is not None
        assert got.prompt == "test"

    def test_get_nonexistent(self, history: "GenerationHistory") -> None:
        """获取不存在的记录返回 None。"""
        assert history.get("nonexistent-id") is None

    def test_update_status(self, history: "GenerationHistory") -> None:
        """更新记录状态。"""
        from services.comfyui_history import GenerationRecord, GenerationStatus
        record = GenerationRecord(
            prompt="test", negative_prompt="", template_name="t"
        )
        history.add(record)
        history.update(record.id, status=GenerationStatus.RUNNING, progress=50)
        updated = history.get(record.id)
        assert updated is not None
        assert updated.status == GenerationStatus.RUNNING
        assert updated.progress == 50

    def test_update_nonexistent(self, history: "GenerationHistory") -> None:
        """更新不存在的记录不报错。"""
        from services.comfyui_history import GenerationStatus
        history.update("nonexistent", status=GenerationStatus.FAILED)

    def test_delete(self, history: "GenerationHistory") -> None:
        """删除记录。"""
        from services.comfyui_history import GenerationRecord
        record = GenerationRecord(
            prompt="test", negative_prompt="", template_name="t"
        )
        history.add(record)
        assert history.delete(record.id) is True
        assert history.get(record.id) is None

    def test_delete_nonexistent(self, history: "GenerationHistory") -> None:
        """删除不存在的记录返回 False。"""
        assert history.delete("nonexistent") is False

    def test_list_records_default(self, history: "GenerationHistory") -> None:
        """列出所有记录（默认分页）。"""
        from services.comfyui_history import GenerationRecord
        for i in range(5):
            history.add(
                GenerationRecord(
                    prompt=f"prompt-{i}", negative_prompt="", template_name="t"
                )
            )
        records, total = history.list_records()
        assert total == 5
        assert len(records) == 5

    def test_list_records_pagination(self, history: "GenerationHistory") -> None:
        """分页列出记录。"""
        from services.comfyui_history import GenerationRecord
        for i in range(10):
            history.add(
                GenerationRecord(
                    prompt=f"prompt-{i}", negative_prompt="", template_name="t"
                )
            )
        records, total = history.list_records(limit=3, offset=0)
        assert total == 10
        assert len(records) == 3

    def test_list_records_filter_by_status(self, history: "GenerationHistory") -> None:
        """按状态过滤记录。"""
        from services.comfyui_history import GenerationRecord, GenerationStatus
        history.add(
            GenerationRecord(
                prompt="running", negative_prompt="", template_name="t",
                status=GenerationStatus.RUNNING,
            )
        )
        history.add(
            GenerationRecord(
                prompt="completed", negative_prompt="", template_name="t",
                status=GenerationStatus.COMPLETED,
            )
        )
        records, total = history.list_records(status="completed")
        assert total == 1
        assert records[0].prompt == "completed"

    def test_persistence(self, tmp_history_file: Path) -> None:
        """数据持久化到文件。"""
        from services.comfyui_history import GenerationHistory, GenerationRecord
        h1 = GenerationHistory(str(tmp_history_file))
        record = GenerationRecord(
            prompt="persist-test", negative_prompt="", template_name="t"
        )
        h1.add(record)
        h2 = GenerationHistory(str(tmp_history_file))
        got = h2.get(record.id)
        assert got is not None
        assert got.prompt == "persist-test"

    def test_corrupted_file_recovery(self, tmp_history_file: Path) -> None:
        """损坏文件自动恢复为空历史。"""
        from services.comfyui_history import GenerationHistory
        tmp_history_file.write_text("not valid json{{{", encoding="utf-8")
        hist = GenerationHistory(str(tmp_history_file))
        records, total = hist.list_records()
        assert total == 0


# ============================================================
# ComfyUIService 测试（Mock 连接器）
# ============================================================


class TestComfyUIService:
    """ComfyUIService 业务逻辑测试。"""

    @pytest.fixture
    def service(self, tmp_workflow_dir: Path, tmp_history_file: Path) -> "ComfyUIService":
        """创建使用临时目录的 ComfyUIService。"""
        from services.comfyui_service import ComfyUIService
        return ComfyUIService(
            workflow_dir=tmp_workflow_dir,
            history_path=str(tmp_history_file),
        )

    def test_list_workflows_empty(self, tmp_path: Path) -> None:
        """空目录返回空列表。"""
        from services.comfyui_service import ComfyUIService
        svc = ComfyUIService(workflow_dir=tmp_path / "empty")
        assert svc.list_workflows() == []

    def test_list_workflows_with_templates(self, service: "ComfyUIService") -> None:
        """列出工作流模板。"""
        templates = service.list_workflows()
        assert len(templates) >= 1
        assert any(t["name"] == "default_txt2img" for t in templates)

    def test_get_workflow(self, service: "ComfyUIService") -> None:
        """获取工作流模板详情。"""
        wf = service.get_workflow("default_txt2img")
        assert "3" in wf
        assert wf["3"]["class_type"] == "KSampler"

    def test_get_workflow_not_found(self, service: "ComfyUIService") -> None:
        """获取不存在的模板抛 FileNotFoundError。"""
        with pytest.raises(FileNotFoundError, match="不存在"):
            service.get_workflow("nonexistent")

    def test_save_and_get_workflow(self, service: "ComfyUIService") -> None:
        """保存后可获取工作流模板。"""
        wf_def = {"node1": {"class_type": "Test", "inputs": {}}}
        service.save_workflow("my_template", wf_def)
        got = service.get_workflow("my_template")
        assert got == wf_def

    def test_delete_workflow(self, service: "ComfyUIService") -> None:
        """删除工作流模板。"""
        wf_def = {"node1": {"class_type": "Test"}}
        service.save_workflow("to_delete", wf_def)
        assert service.delete_workflow("to_delete") is True
        with pytest.raises(FileNotFoundError):
            service.get_workflow("to_delete")

    def test_delete_workflow_not_found(self, service: "ComfyUIService") -> None:
        """删除不存在的模板抛 FileNotFoundError。"""
        with pytest.raises(FileNotFoundError):
            service.delete_workflow("nonexistent")

    def test_get_status_not_connected(self, service: "ComfyUIService") -> None:
        """未连接时返回 disconnected 状态。"""
        status = service.get_status()
        assert status["connected"] is False

    async def test_connect_failure(self, service: "ComfyUIService") -> None:
        """连接失败返回错误。"""
        with patch("services.comfyui_service.ComfyUIConnector") as MockConnector:
            mock_instance = MagicMock()
            mock_instance.is_connected = False
            mock_instance.connect = AsyncMock()
            MockConnector.return_value = mock_instance
            result = await service.connect("http://bad-host:8188")
            assert result["connected"] is False

    async def test_connect_success(self, service: "ComfyUIService") -> None:
        """连接成功返回状态信息。"""
        with patch("services.comfyui_service.ComfyUIConnector") as MockConnector:
            from connectors.types import ConnectorState
            mock_instance = MagicMock()
            mock_instance.is_connected = True
            mock_instance.state = ConnectorState.CONNECTED
            mock_instance.connect = AsyncMock()
            mock_instance.start_ws_listener = AsyncMock()
            MockConnector.return_value = mock_instance
            result = await service.connect("http://localhost:8188")
            assert result["connected"] is True
            assert result["endpoint"] == "http://localhost:8188"

    async def test_generate_no_connection(self, service: "ComfyUIService") -> None:
        """未连接时调用 generate 抛 RuntimeError。"""
        with pytest.raises(RuntimeError, match="未连接"):
            await service.generate(prompt="test")

    async def test_generate_workflow_not_found(self, service: "ComfyUIService") -> None:
        """模板不存在时 generate 抛 FileNotFoundError。"""
        with patch.object(service, "_require_connector"):
            with pytest.raises(FileNotFoundError, match="不存在"):
                await service.generate(prompt="test", template="missing_template")

    async def test_generate_submit_success(self, service: "ComfyUIService") -> None:
        """成功提交生成任务。"""
        mock_connector = MagicMock()
        mock_connector.is_connected = True
        mock_result = MagicMock()
        mock_result.success = True
        mock_result.data = {"prompt_id": "pid-123"}
        mock_connector.execute_action = AsyncMock(return_value=mock_result)
        service._connector = mock_connector
        record = await service.generate(prompt="a beautiful sunset")
        assert record.prompt == "a beautiful sunset"
        assert record.id

    async def test_generate_submit_failure(self, service: "ComfyUIService") -> None:
        """提交工作流失败时记录为 FAILED。"""
        mock_connector = MagicMock()
        mock_connector.is_connected = True
        mock_result = MagicMock()
        mock_result.success = False
        mock_result.error = "server error"
        mock_connector.execute_action = AsyncMock(return_value=mock_result)
        service._connector = mock_connector
        record = await service.generate(prompt="test")
        from services.comfyui_history import GenerationStatus
        assert record.status == GenerationStatus.FAILED
        assert record.error == "server error"

    def test_get_task_progress_not_found(self, service: "ComfyUIService") -> None:
        """不存在记录的进度返回 None。"""
        assert service.get_task_progress("nonexistent") is None

    def test_history_property(self, service: "ComfyUIService") -> None:
        """history 属性返回 GenerationHistory 实例。"""
        from services.comfyui_history import GenerationHistory
        assert isinstance(service.history, GenerationHistory)


# ============================================================
# API 路由测试（httpx AsyncClient）
# ============================================================


class TestComfyUIRoutes:
    """ComfyUI API 路由集成测试。"""

    @pytest.fixture
    def mock_service(self) -> MagicMock:
        """创建 Mock ComfyUIService。"""
        svc = MagicMock()
        svc.get_status.return_value = {
            "connected": False,
            "endpoint": None,
            "state": "disconnected",
        }
        svc.list_workflows.return_value = [
            {"name": "default_txt2img", "file_path": "/workflows/default_txt2img.json"}
        ]
        svc.get_workflow.return_value = {"node1": {"class_type": "KSampler"}}
        svc.history = MagicMock()
        return svc

    @pytest.fixture
    async def client(self, mock_service: MagicMock, tmp_path: Path) -> "AsyncClient":
        """创建测试用 httpx AsyncClient。"""
        from httpx import ASGITransport, AsyncClient

        with patch(
            "channels.api.routes_comfyui.get_comfyui_service",
            return_value=mock_service,
        ):
            from channels.api.app import create_app
            app = create_app()
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as c:
                yield c

    async def test_get_status(self, client: "AsyncClient", mock_service: MagicMock) -> None:
        """GET /api/v1/comfyui/status 返回连接状态。"""
        resp = await client.get("/api/v1/comfyui/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["connected"] is False

    async def test_list_workflows(self, client: "AsyncClient", mock_service: MagicMock) -> None:
        """GET /api/v1/comfyui/workflows 列出模板。"""
        resp = await client.get("/api/v1/comfyui/workflows")
        assert resp.status_code == 200
        data = resp.json()
        assert "templates" in data
        assert data["total"] >= 1

    async def test_get_workflow_detail(self, client: "AsyncClient", mock_service: MagicMock) -> None:
        """GET /api/v1/comfyui/workflows/{name} 获取模板详情。"""
        resp = await client.get("/api/v1/comfyui/workflows/default_txt2img")
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "default_txt2img"

    async def test_get_workflow_not_found(self, client: "AsyncClient", mock_service: MagicMock) -> None:
        """GET 模板不存在返回 404。"""
        mock_service.get_workflow.side_effect = FileNotFoundError("not found")
        resp = await client.get("/api/v1/comfyui/workflows/nonexistent")
        assert resp.status_code == 404

    async def test_connect(self, client: "AsyncClient", mock_service: MagicMock) -> None:
        """POST /api/v1/comfyui/connect 连接 ComfyUI。"""
        mock_service.connect = AsyncMock(
            return_value={"connected": True, "endpoint": "http://localhost:8188"}
        )
        resp = await client.post(
            "/api/v1/comfyui/connect",
            json={"endpoint": "http://localhost:8188"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["connected"] is True

    async def test_connect_failure(self, client: "AsyncClient", mock_service: MagicMock) -> None:
        """POST connect 连接失败返回 503。"""
        mock_service.connect = AsyncMock(
            return_value={"connected": False, "error": "timeout"}
        )
        resp = await client.post(
            "/api/v1/comfyui/connect",
            json={"endpoint": "http://bad:8188"},
        )
        assert resp.status_code == 503

    async def test_disconnect(self, client: "AsyncClient", mock_service: MagicMock) -> None:
        """POST /api/v1/comfyui/disconnect 断开连接。"""
        mock_service.disconnect = AsyncMock(
            return_value={"connected": False, "message": "已断开"}
        )
        resp = await client.post("/api/v1/comfyui/disconnect")
        assert resp.status_code == 200

    async def test_save_workflow(self, client: "AsyncClient", mock_service: MagicMock) -> None:
        """POST /api/v1/comfyui/workflows 保存模板。"""
        mock_service.save_workflow.return_value = {"name": "test", "file_path": "/test.json"}
        resp = await client.post(
            "/api/v1/comfyui/workflows",
            json={"name": "test", "workflow": {"node": {}}},
        )
        assert resp.status_code == 200

    async def test_delete_workflow(self, client: "AsyncClient", mock_service: MagicMock) -> None:
        """DELETE /api/v1/comfyui/workflows/{name} 删除模板。"""
        resp = await client.delete("/api/v1/comfyui/workflows/test")
        assert resp.status_code == 200

    async def test_delete_workflow_not_found(self, client: "AsyncClient", mock_service: MagicMock) -> None:
        """DELETE 模板不存在返回 404。"""
        mock_service.delete_workflow.side_effect = FileNotFoundError("not found")
        resp = await client.delete("/api/v1/comfyui/workflows/nonexistent")
        assert resp.status_code == 404

    async def test_list_models(self, client: "AsyncClient", mock_service: MagicMock) -> None:
        """GET /api/v1/comfyui/models 列出模型。"""
        mock_service.list_models = AsyncMock(
            return_value={"checkpoints": ["model.safetensors"]}
        )
        resp = await client.get("/api/v1/comfyui/models")
        assert resp.status_code == 200
        data = resp.json()
        assert "checkpoints" in data["models"]

    async def test_generate(self, client: "AsyncClient", mock_service: MagicMock) -> None:
        """POST /api/v1/comfyui/generate 提交生成任务。"""
        from services.comfyui_history import GenerationRecord
        mock_record = GenerationRecord(
            prompt="a cat", negative_prompt="", template_name="default_txt2img"
        )
        mock_service.generate = AsyncMock(return_value=mock_record)
        resp = await client.post(
            "/api/v1/comfyui/generate",
            json={"prompt": "a cat"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["record"]["prompt"] == "a cat"

    async def test_generate_no_connection(self, client: "AsyncClient", mock_service: MagicMock) -> None:
        """POST generate 未连接返回 503。"""
        mock_service.generate = AsyncMock(side_effect=RuntimeError("未连接"))
        resp = await client.post(
            "/api/v1/comfyui/generate",
            json={"prompt": "test"},
        )
        assert resp.status_code == 503

    async def test_get_history(self, client: "AsyncClient", mock_service: MagicMock) -> None:
        """GET /api/v1/comfyui/history 获取历史记录。"""
        mock_service.history.list_records.return_value = ([], 0)
        resp = await client.get("/api/v1/comfyui/history")
        assert resp.status_code == 200
        data = resp.json()
        assert "records" in data
        assert "total" in data

    async def test_get_history_record_not_found(self, client: "AsyncClient", mock_service: MagicMock) -> None:
        """GET history/{id} 记录不存在返回 404。"""
        mock_service.history.get.return_value = None
        resp = await client.get("/api/v1/comfyui/history/nonexistent-id")
        assert resp.status_code == 404

    async def test_delete_history_record(self, client: "AsyncClient", mock_service: MagicMock) -> None:
        """DELETE history/{id} 删除记录。"""
        mock_service.history.delete.return_value = True
        resp = await client.delete("/api/v1/comfyui/history/rec-123")
        assert resp.status_code == 200

    async def test_delete_history_record_not_found(self, client: "AsyncClient", mock_service: MagicMock) -> None:
        """DELETE history/{id} 记录不存在返回 404。"""
        mock_service.history.delete.return_value = False
        resp = await client.delete("/api/v1/comfyui/history/rec-123")
        assert resp.status_code == 404

    async def test_get_task_progress(self, client: "AsyncClient", mock_service: MagicMock) -> None:
        """GET tasks/{id}/progress 获取进度。"""
        mock_service.get_task_progress.return_value = {
            "id": "rec-1", "status": "running", "progress": 50, "prompt_id": "pid-1",
        }
        resp = await client.get("/api/v1/comfyui/tasks/rec-1/progress")
        assert resp.status_code == 200
        assert resp.json()["progress"] == 50

    async def test_cancel_task(self, client: "AsyncClient", mock_service: MagicMock) -> None:
        """POST tasks/{id}/cancel 取消任务。"""
        mock_service.cancel_task = AsyncMock(return_value=True)
        resp = await client.post("/api/v1/comfyui/tasks/rec-1/cancel")
        assert resp.status_code == 200
        assert resp.json()["success"] is True

    async def test_cancel_task_not_running(self, client: "AsyncClient", mock_service: MagicMock) -> None:
        """POST tasks/{id}/cancel 任务不在运行返回 400。"""
        mock_service.cancel_task = AsyncMock(return_value=False)
        resp = await client.post("/api/v1/comfyui/tasks/rec-1/cancel")
        assert resp.status_code == 400
