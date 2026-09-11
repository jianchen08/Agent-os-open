# @feature: FP-0.2.二 内部模块统一 manifest 化 | @vision: V3 可嵌入 | @ci: python-coverage
"""fs_tools 工作空间约束测试（punch B5）。

project_root 前缀校验（参考 download/tool.py 的 WorkspaceAwareMixin 语义）：
- 读写同规：workspace 外绝对路径一律拒绝（读不豁免）；
- 凭据类文件（.env 族/SSH 私钥/证书私钥）硬拒，与根内外无关，
  .env.example 豁免；
- workspace 内路径（含相对路径解析）通过；
- 未注入 workspace/project_root 时 fail-closed 报错（不做 cwd 兜底）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentos_builtin_tools import fs_tools
from agentos_builtin_tools.fs_tools import (
    copy_file,
    create_directory,
    delete_file,
    file_read,
    file_write,
    list_directory,
    move_file,
)
from agentos_builtin_tools.search_tool import enhanced_search

pytestmark = pytest.mark.unit


class TestFileWriteWorkspaceConstraint:
    async def test_write_outside_workspace_rejected(self, tmp_path: Path) -> None:
        """workspace 外绝对路径写入被拒，文件不被创建。"""
        ws = tmp_path / "ws"
        ws.mkdir()
        outside = tmp_path / "outside.txt"

        result = await file_write(
            path=str(outside), action="write", content="evil",
            workspace=str(ws),
        )
        assert result.success is False
        assert "超出 workspace/project_root" in result.error
        assert not outside.exists()

    async def test_write_inside_workspace_allowed(self, tmp_path: Path) -> None:
        """workspace 内写入通过。"""
        ws = tmp_path / "ws"
        ws.mkdir()
        target = ws / "note.md"

        result = await file_write(
            path=str(target), action="write", content="hello",
            workspace=str(ws),
        )
        assert result.success is True
        assert target.read_text(encoding="utf-8") == "hello"
        # file 字段 = 写盘后的绝对路径（卡片打开坐标契约）
        assert Path(result.output["file"]) == target.resolve()

    async def test_write_relative_path_resolved_in_workspace(self, tmp_path: Path) -> None:
        """相对路径以 workspace 为基准解析，落在根内则通过。"""
        ws = tmp_path / "ws"
        ws.mkdir()

        result = await file_write(
            path="rel.txt", action="write", content="ok", workspace=str(ws),
        )
        assert result.success is True
        assert (ws / "rel.txt").read_text(encoding="utf-8") == "ok"
        assert Path(result.output["file"]) == (ws / "rel.txt").resolve()

    async def test_write_traversal_escape_rejected(self, tmp_path: Path) -> None:
        """相对路径含 .. 逃逸出 workspace 被拒。"""
        ws = tmp_path / "ws"
        ws.mkdir()

        result = await file_write(
            path="../escape.txt", action="write", content="evil", workspace=str(ws),
        )
        assert result.success is False
        assert not (tmp_path / "escape.txt").exists()

    async def test_write_without_workspace_context_rejected(self, tmp_path: Path) -> None:
        """未注入 workspace/project_root：fail-closed 拒绝（无 cwd 兜底）。"""
        target = tmp_path / "free.txt"
        result = await file_write(path=str(target), action="write", content="x")
        assert result.success is False
        assert "未注入" in result.error
        assert not target.exists()

    async def test_write_relative_path_without_injection_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """未注入 workspace 且传相对路径：拒绝——相对路径禁止以进程 cwd 解析。

        （历史 bug：sidecar cwd = 插件目录，相对路径在插件树里创建文件。）
        """
        monkeypatch.chdir(tmp_path)
        result = await file_write(path="rel.txt", action="write", content="ok")
        assert result.success is False
        assert not (tmp_path / "rel.txt").exists()


class TestFileWriteCreatesParentDirs:
    """file_write 写文件语义自包含：目标父目录缺失时自动创建。

    agents 配置常把过程文档写到 docs/working/ 等深层路径，全新隔离工作区
    该目录不存在；write/append/insert 不建父目录时 write_text 直接抛
    errno 2（IO error），逼 agent 绕道 create_directory 才能落盘。
    """

    @pytest.mark.parametrize(
        ("action", "extra_kwargs", "expected_content"),
        [
            ("write", {}, "hello"),
            ("append", {}, "hello"),
            ("insert", {"line": 0}, "hello\n"),
        ],
    )
    async def test_missing_parent_dirs_auto_created(
        self, tmp_path: Path, action: str, extra_kwargs: dict, expected_content: str
    ) -> None:
        """父目录不存在时自动建目录并落盘，file 字段回宿主绝对路径。"""
        ws = tmp_path / "ws"
        ws.mkdir()
        target = ws / "docs" / "working" / "note.md"

        result = await file_write(
            path=str(target),
            action=action,
            content="hello",
            workspace=str(ws),
            **extra_kwargs,
        )

        assert result.success is True, result.error
        assert target.read_text(encoding="utf-8") == expected_content
        # 性质：落盘坐标落在 workspace 内且为绝对路径（卡片打开契约）
        assert Path(result.output["file"]) == target.resolve()

    async def test_deeply_nested_missing_dirs_created(self, tmp_path: Path) -> None:
        """多级缺失目录一次性建全（parents 语义）。"""
        ws = tmp_path / "ws"
        ws.mkdir()
        target = ws / "a" / "b" / "c" / "d.txt"

        result = await file_write(
            path=str(target), action="write", content="deep", workspace=str(ws),
        )

        assert result.success is True, result.error
        assert target.read_text(encoding="utf-8") == "deep"

    async def test_search_replace_keeps_existing_file_semantics(self, tmp_path: Path) -> None:
        """search_replace 只改已存在文件：仍报 File not found，且不副作用建目录。"""
        ws = tmp_path / "ws"
        ws.mkdir()
        target = ws / "ghost" / "f.txt"

        result = await file_write(
            path=str(target), action="search_replace",
            old_str="a", new_str="b", workspace=str(ws),
        )

        assert result.success is False
        assert "File not found" in result.error
        assert not (ws / "ghost").exists()


class TestMoveFileWorkspaceConstraint:
    async def test_move_destination_outside_rejected(self, tmp_path: Path) -> None:
        """目标在 workspace 外：移动被拒，源文件保留原地。"""
        ws = tmp_path / "ws"
        ws.mkdir()
        src = ws / "a.txt"
        src.write_text("data", encoding="utf-8")
        outside = tmp_path / "moved.txt"

        result = await move_file(
            source=str(src), destination=str(outside), workspace=str(ws),
        )
        assert result.success is False
        assert "超出 workspace/project_root" in result.error
        assert src.exists()
        assert not outside.exists()

    async def test_move_inside_workspace_allowed(self, tmp_path: Path) -> None:
        """workspace 内移动通过。"""
        ws = tmp_path / "ws"
        ws.mkdir()
        src = ws / "a.txt"
        src.write_text("data", encoding="utf-8")
        dst = ws / "b.txt"

        result = await move_file(
            source=str(src), destination=str(dst), workspace=str(ws),
        )
        assert result.success is True
        assert dst.read_text(encoding="utf-8") == "data"
        assert not src.exists()


class TestDeleteFileWorkspaceConstraint:
    async def test_delete_outside_workspace_rejected(self, tmp_path: Path) -> None:
        """workspace 外删除被拒，目标保留。"""
        ws = tmp_path / "ws"
        ws.mkdir()
        outside = tmp_path / "precious.txt"
        outside.write_text("keep", encoding="utf-8")

        result = await delete_file(path=str(outside), workspace=str(ws))
        assert result.success is False
        assert "超出 workspace/project_root" in result.error
        assert outside.exists()

    async def test_delete_inside_workspace_allowed(self, tmp_path: Path) -> None:
        """workspace 内删除通过。"""
        ws = tmp_path / "ws"
        ws.mkdir()
        target = ws / "junk.txt"
        target.write_text("junk", encoding="utf-8")

        result = await delete_file(path=str(target), workspace=str(ws))
        assert result.success is True
        assert not target.exists()

    async def test_delete_batch_checks_every_path(self, tmp_path: Path) -> None:
        """批量删除逐条校验：任一路径越界即整体拒绝。"""
        ws = tmp_path / "ws"
        ws.mkdir()
        inside = ws / "ok.txt"
        inside.write_text("ok", encoding="utf-8")
        outside = tmp_path / "no.txt"
        outside.write_text("no", encoding="utf-8")

        result = await delete_file(
            paths=[str(inside), str(outside)], workspace=str(ws),
        )
        assert result.success is False
        assert inside.exists()
        assert outside.exists()


class TestFileReadReturnsResolvedPath:
    """file_read 的 file 字段返回宿主侧绝对路径（工具卡片打开文件的坐标契约）。

    隔离任务下 agent 以容器挂载点 /workspace 为 cwd（isolation_guard 固定挂载），
    传相对路径或 /workspace/* 容器路径；前端工具卡片按 file 字段到宿主文件系统
    读取（get_file_content 绝对路径直读），原样回传将打不开。
    _check_workspace_path 已完成容器路径重映射 + 根锚定（file_write 同款消费其
    返回值），file_read 的读取与回传都必须使用该结果。
    """

    async def test_container_mount_path_reads_and_returns_host_path(self, tmp_path: Path) -> None:
        """容器挂载路径 /workspace/* 重映射到宿主工作区：读取成功且 file 为宿主路径。"""
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "report.md").write_text("hello\n", encoding="utf-8")

        result = await file_read(path="/workspace/report.md", workspace=str(ws))

        assert result.success is True
        assert Path(result.output["file"]) == ws / "report.md"
        assert result.output["content"] == "hello\n"

    async def test_relative_path_resolved_to_workspace_root(self, tmp_path: Path) -> None:
        """相对路径锚定工作区根：file 返回宿主绝对路径（性质：绝对且落在根内）。"""
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "doc.txt").write_text("line\n", encoding="utf-8")

        result = await file_read(path="doc.txt", workspace=str(ws))

        assert result.success is True
        resolved = Path(result.output["file"])
        assert resolved.is_absolute()
        assert resolved == ws / "doc.txt"

    async def test_without_injection_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """未注入 workspace/project_root：fail-closed 拒绝（无 cwd 兜底）。"""
        target = tmp_path / "free.txt"
        target.write_text("x\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)

        result = await file_read(path="free.txt")

        assert result.success is False
        assert "未注入" in result.error

    async def test_absolute_outside_path_rejected(self, tmp_path: Path) -> None:
        """根外绝对路径读取拒绝（与写同规 fail-closed），不返回文件内容。"""
        ws = tmp_path / "ws"
        ws.mkdir()
        outside = tmp_path / "cfg.ini"
        outside.write_text("[a]\n", encoding="utf-8")

        result = await file_read(path=str(outside), workspace=str(ws))

        assert result.success is False
        assert "超出 workspace/project_root" in result.error


class TestSensitiveFileDeny:
    """凭据类文件硬拒：与根内外无关，读写同规（.env.example 豁免）。"""

    async def test_read_env_inside_workspace_denied(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        env_file = ws / ".env"
        env_file.write_text("API_KEY=x\n", encoding="utf-8")

        result = await file_read(path=str(env_file), workspace=str(ws))

        assert result.success is False
        assert "凭据类文件" in result.error

    async def test_read_env_example_allowed(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        example = ws / ".env.example"
        example.write_text("API_KEY=\n", encoding="utf-8")

        result = await file_read(path=str(example), workspace=str(ws))

        assert result.success is True

    async def test_read_env_dot_variant_denied(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        variant = ws / ".env.production"
        variant.write_text("API_KEY=x\n", encoding="utf-8")

        result = await file_read(path=str(variant), workspace=str(ws))

        assert result.success is False
        assert "凭据类文件" in result.error

    async def test_write_env_inside_workspace_denied(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()

        result = await file_write(path=str(ws / ".env"), content="API_KEY=x\n", workspace=str(ws))

        assert result.success is False
        assert "凭据类文件" in result.error
        assert not (ws / ".env").exists()

    @pytest.mark.parametrize("name", ["server.pem", "keystore.p12", "cert.pfx", "app.jks", "id_rsa", "id_ed25519"])
    async def test_read_key_files_denied(self, tmp_path: Path, name: str) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        target = ws / name
        target.write_text("secret\n", encoding="utf-8")

        result = await file_read(path=str(target), workspace=str(ws))

        assert result.success is False
        assert "凭据类文件" in result.error

    async def test_move_env_source_denied(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        env_file = ws / ".env"
        env_file.write_text("API_KEY=x\n", encoding="utf-8")

        result = await move_file(source=str(env_file), destination=str(ws / "leak.txt"), workspace=str(ws))

        assert result.success is False
        assert env_file.exists()

    async def test_normal_file_unaffected(self, tmp_path: Path) -> None:
        """非凭据文件不受敏感名单影响（防误伤性质断言）。"""
        ws = tmp_path / "ws"
        ws.mkdir()
        target = ws / "environment.txt"
        target.write_text("ok\n", encoding="utf-8")

        result = await file_read(path=str(target), workspace=str(ws))

        assert result.success is True

    async def test_read_inside_workspace_no_warning(self, tmp_path: Path) -> None:
        """workspace 内读取正常，不产生越界告警。"""
        ws = tmp_path / "ws"
        ws.mkdir()
        target = ws / "doc.txt"
        target.write_text("line1\n", encoding="utf-8")

        result = await file_read(path=str(target), workspace=str(ws))
        assert result.success is True

class TestPathToolsWorkspaceAnchoring:
    """list_directory / create_directory / copy_file / enhanced_search 锚定契约。

    历史 bug：这些工具签名不声明 workspace/project_root，SDK 分发层按签名
    过滤把注入值静默丢弃，相对路径以 sidecar cwd 解析——agent 在插件目录里
    浏览/创建目录（目录飘移）。现签名声明注入参数 + 无注入 fail-closed。
    """

    async def test_create_directory_relative_anchored_to_workspace(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()

        result = await create_directory("docs/working", workspace=str(ws))

        assert result.success is True
        # 性质：落盘路径必须落在 workspace 内（不得逃逸到 cwd/插件目录）
        created = Path(result.output["path"])
        assert created.is_absolute()
        assert created.relative_to(ws.resolve()) == Path("docs") / "working"
        assert (ws / "docs" / "working").is_dir()

    async def test_create_directory_container_mount_remapped(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()

        result = await create_directory("/workspace/out", workspace=str(ws))

        assert result.success is True
        assert (ws / "out").is_dir()

    async def test_create_directory_without_injection_rejected(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        result = await create_directory("docs/working")
        assert result.success is False
        assert "未注入" in result.error
        assert not (tmp_path / "docs").exists()

    async def test_list_directory_relative_anchored(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "a.txt").write_text("x", encoding="utf-8")

        result = await list_directory(".", workspace=str(ws))

        assert result.success is True
        names = {item["name"] for item in result.output["items"]}
        assert names == {"a.txt"}

    async def test_list_directory_without_injection_rejected(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "a.txt").write_text("x", encoding="utf-8")
        result = await list_directory(".")
        assert result.success is False
        assert "未注入" in result.error

    async def test_copy_file_anchored_both_ends(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "src.txt").write_text("data", encoding="utf-8")

        result = await copy_file("src.txt", "dst.txt", workspace=str(ws))

        assert result.success is True
        assert (ws / "dst.txt").read_text(encoding="utf-8") == "data"

    async def test_copy_file_without_injection_rejected(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "src.txt").write_text("data", encoding="utf-8")
        result = await copy_file("src.txt", "dst.txt")
        assert result.success is False
        assert "未注入" in result.error
        assert not (tmp_path / "dst.txt").exists()

    async def test_enhanced_search_relative_anchored(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "code.py").write_text("def needle(): pass" + chr(10), encoding="utf-8")

        result = await enhanced_search("needle", path=".", workspace=str(ws))

        assert result.success is True
        assert len(result.output["results"]) == 1
        assert Path(result.output["results"][0]["file_path"]) == ws / "code.py"

    async def test_enhanced_search_without_injection_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        result = await enhanced_search("x", path=".")
        assert result.success is False
        assert "未注入" in result.error


class TestSensitiveFileNameNormalization:
    """S3：凭据名单比较大小写/尾随字符归一（Windows FS 同名等价）。"""

    @pytest.mark.parametrize("name", [".ENV", ".Env", ".env ", ".env.", ".ENV."])
    def test_env_case_and_trailing_variants_denied(self, tmp_path: Path, name: str) -> None:
        reason = fs_tools._sensitive_file_reason(tmp_path / name)
        assert reason is not None, f"{name!r} 与 .env 在宿主 FS 同名等价，必须拒"

    @pytest.mark.parametrize("name", ["ID_RSA", "id_ed25519.pub".replace("pub", "PEM"), "server.PEM"])
    def test_key_case_variants_denied(self, tmp_path: Path, name: str) -> None:
        reason = fs_tools._sensitive_file_reason(tmp_path / name)
        assert reason is not None, f"{name!r} 是既有凭据名的大小写变体，必须拒"

    def test_env_example_still_allowed_after_normalization(self, tmp_path: Path) -> None:
        assert fs_tools._sensitive_file_reason(tmp_path / ".env.example") is None
        assert fs_tools._sensitive_file_reason(tmp_path / ".ENV.EXAMPLE") is None

    def test_normal_files_unaffected(self, tmp_path: Path) -> None:
        assert fs_tools._sensitive_file_reason(tmp_path / "readme.md") is None
        assert fs_tools._sensitive_file_reason(tmp_path / "environment") is None
