# @feature: FP-0.2.二 内部模块 manifest(BUG-57 /uploads/ 读面放行) | @ci: python-coverage
"""uploads 附件只读放行测试（BUG-57：子任务读不到 /uploads/ 文件）。

锁定契约（fs_tools._check_workspace_path 单点判定，共用方 file_read /
list_directory / enhanced_search）：
1. ``/uploads/{filename}`` 引用 → 解析到当前租户 uploads 落盘目录，纯读
   允许（复用共享解析点 uploads_path.resolve_uploads_url，与上传落盘 /
   内核静态服务 / L1 附件注入三方同源）；
2. 写/删/move 对 /uploads/ 前缀维持拒绝（只读放行，写保护回归）；
3. 租户隔离：/uploads/{name} 恒映射进本租户 uploads 目录，他租户文件
   不可命名（basename 拼接，``..`` 目录穿越在形态上被拒绝）；
4. 凭据黑名单恒先行（/uploads/ 域不豁免）；白名单外路径仍拒绝。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import repo_anchor

from agentos_builtin_tools.fs_tools import delete_file, file_read, file_write, move_file

pytestmark = pytest.mark.unit


@pytest.fixture()
def uploads_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """租户数据根钉到 tmp（AGENTOS_DATA_DIR）+ 空会话工作空间 + 双租户布局。

    tenant_data 经 env 解析数据根（上传落盘/读面同源）；默认租户 default 的
    uploads 预置一个附件，第二租户 tenant_b 的 uploads 预置同名文件——
    跨租户不可达的判定素材。空白名单目录钉住 AGENTOS_CONFIG_USERS_DIR，
    与登记侧测试同款隔离。
    """
    data_base = tmp_path / "udata"
    default_uploads = data_base / "default" / "uploads"
    default_uploads.mkdir(parents=True)
    (default_uploads / "a1b2c3.txt").write_text("MEETING NOTES\n", encoding="utf-8")
    tenant_b_uploads = data_base / "tenant_b" / "uploads"
    tenant_b_uploads.mkdir(parents=True)
    (tenant_b_uploads / "a1b2c3.txt").write_text("TENANT_B SECRET\n", encoding="utf-8")
    secret = data_base / "secret.txt"
    secret.write_text("OUTSIDE UPLOADS\n", encoding="utf-8")
    ws = tmp_path / "workspace" / "task-1"  # 子任务工作空间（uploads 在其外）
    ws.mkdir(parents=True)
    users_dir = tmp_path / "config" / "users"
    (users_dir / "default").mkdir(parents=True)
    monkeypatch.setenv("AGENTOS_DATA_DIR", str(data_base))
    monkeypatch.setenv("AGENTOS_CONFIG_USERS_DIR", str(users_dir))
    repo_anchor.reset_cache()
    yield SimpleNamespace(
        data_base=data_base,
        uploads=default_uploads,
        tenant_b_uploads=tenant_b_uploads,
        secret=secret,
        ws=ws,
    )
    repo_anchor.reset_cache()


class TestUploadsReadAllow:
    """/uploads/{filename} 纯读放行（BUG-57 主场景）。"""

    async def test_subtask_reads_uploaded_attachment(
        self, uploads_env: SimpleNamespace
    ) -> None:
        """子任务工作空间读 /uploads/{hex}.txt 成功，file 字段回宿主落盘路径。"""
        result = await file_read(path="/uploads/a1b2c3.txt", workspace=str(uploads_env.ws))

        assert result.success is True, result.error
        assert "MEETING NOTES" in result.output["content"]
        assert Path(result.output["file"]) == (uploads_env.uploads / "a1b2c3.txt").resolve()

    async def test_read_maps_into_current_tenant_uploads_dir(
        self, uploads_env: SimpleNamespace
    ) -> None:
        """解析落点 = 数据根 {tenant}/uploads（上传落盘同源，非进程 cwd 拼接）。"""
        result = await file_read(path="/uploads/a1b2c3.txt", project_root=str(uploads_env.ws))

        assert result.success is True, result.error
        assert str(uploads_env.uploads) in result.output["file"]


class TestUploadsWriteProtection:
    """写/删/move 对 /uploads/ 前缀维持拒绝（只读放行）。"""

    async def test_write_uploads_rejected(self, uploads_env: SimpleNamespace) -> None:
        """file_write /uploads/ 拒绝，附件内容不被改动。"""
        target = uploads_env.uploads / "a1b2c3.txt"
        before = target.read_text(encoding="utf-8")

        result = await file_write(
            path="/uploads/a1b2c3.txt",
            action="append",
            content="evil",
            workspace=str(uploads_env.ws),
        )

        assert result.success is False
        assert "超出 workspace/project_root" in result.error
        assert target.read_text(encoding="utf-8") == before

    @pytest.mark.parametrize("op", ["delete", "move"])
    async def test_mutating_ops_uploads_rejected(
        self, uploads_env: SimpleNamespace, op: str
    ) -> None:
        """delete/move 同规拒绝（uploads 对 agent 是只读域）。"""
        target = uploads_env.uploads / "a1b2c3.txt"

        if op == "delete":
            result = await delete_file(path="/uploads/a1b2c3.txt", workspace=str(uploads_env.ws))
        else:
            result = await move_file(
                source="/uploads/a1b2c3.txt",
                destination=str(uploads_env.ws / "moved.txt"),
                workspace=str(uploads_env.ws),
            )

        assert result.success is False
        assert target.exists()


class TestUploadsTenantIsolation:
    """租户隔离：/uploads/{name} 恒映射进本租户 uploads，跨租户不可命名。"""

    async def test_default_tenant_scoped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """default 租户上下文读 /uploads/{name}：tenant_b 同名文件不可达。"""
        data_base = tmp_path / "ud"
        (data_base / "default" / "uploads").mkdir(parents=True)
        tenant_b = data_base / "tenant_b" / "uploads"
        tenant_b.mkdir(parents=True)
        (tenant_b / "x9y8.txt").write_text("B ONLY\n", encoding="utf-8")
        ws = tmp_path / "ws"
        ws.mkdir()
        monkeypatch.setenv("AGENTOS_DATA_DIR", str(data_base))
        monkeypatch.setenv("AGENTOS_CONFIG_USERS_DIR", str(tmp_path / "cfg" / "users"))
        repo_anchor.reset_cache()
        try:
            result = await file_read(path="/uploads/x9y8.txt", workspace=str(ws))

            assert result.success is False
            assert "File not found" in result.error
            assert "B ONLY" not in (result.output or {}).get("content", "")
            assert str(tenant_b) not in (result.output or {}).get("file", "")
        finally:
            repo_anchor.reset_cache()

    async def test_traversal_escape_not_rescued(self, uploads_env: SimpleNamespace) -> None:
        """``..`` 穿越形态被 basename 拼接拒绝：uploads 外 secret 零泄露。"""
        result = await file_read(
            path="/uploads/../../secret.txt", workspace=str(uploads_env.ws)
        )

        assert result.success is False
        assert "OUTSIDE UPLOADS" not in (result.output or {}).get("content", "")


class TestUploadsGuardsUnchanged:
    """凭据黑名单恒先行 + 白名单外路径仍拒绝（读放宽不等于任意读）。"""

    async def test_sensitive_name_under_uploads_denied(
        self, uploads_env: SimpleNamespace
    ) -> None:
        """/uploads/ 域不豁免凭据黑名单：id_rsa 命名仍拒。"""
        (uploads_env.uploads / "id_rsa").write_text("PRIVATE KEY\n", encoding="utf-8")

        result = await file_read(path="/uploads/id_rsa", workspace=str(uploads_env.ws))

        assert result.success is False
        assert "凭据类文件" in result.error

    async def test_outside_uploads_prefix_readable_under_denylist(
        self, uploads_env: SimpleNamespace
    ) -> None:
        """读黑名单制：非 /uploads/ 的根外普通文件读放行（附件锚只管 /uploads/ 形）。"""
        result = await file_read(path=str(uploads_env.secret), workspace=str(uploads_env.ws))

        assert result.success is True, result.error
        assert "OUTSIDE UPLOADS" in result.output["content"]
