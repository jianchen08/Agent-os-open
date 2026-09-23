# @feature: FP-0.2.spill_guard 内置工具读护栏(R171 读面白名单前缀锚) | @ci: python-coverage
"""读面白名单前缀锚测试（2026-09-19 用户裁定：读是宽的，写/删才限工作空间）。

锁定契约（file_read / list_directory / enhanced_search 共用
fs_tools._check_workspace_path 单点判定）：
1. 相对路径以工作空间解析落空 → 按白名单前缀根回退解析一次（R171 主场景：
   L1 会话空工作空间读仓库相对路径不再 File not found）；
2. 工作空间外路径落在白名单前缀内 → 纯读允许；白名单外仍拒绝；
3. 仓库锚拒绝目录（config 等）不因白名单放行，凭据黑名单恒先行；
4. 写/删/move 对白名单内工作空间外路径维持拒绝（写保护回归）；
5. 工作空间内已存在文件优先（回退只在落空时发生）。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import repo_anchor
import yaml

from agentos_builtin_tools.fs_tools import (
    delete_file,
    file_read,
    file_write,
    list_directory,
    move_file,
)
from agentos_builtin_tools.search_tool import enhanced_search

pytestmark = pytest.mark.unit

_HELLO_REL = "plugins/shared/tools/hello_pack/hello_pack.py"


@pytest.fixture()
def whitelist_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """假仓库（repo 锚标记）+ 第二白名单前缀 + 空会话工作空间 + 白名单文件。

    白名单经 env AGENTOS_CONFIG_USERS_DIR 钉到 tmp（与登记侧测试同款隔离）；
    repo 锚经 AGENTOS_CONFIG_ROOT 钉到假仓库并重置解析缓存。
    """
    repo = tmp_path / "repo"
    (repo / "config" / "kernel").mkdir(parents=True)
    hello = repo / "plugins" / "shared" / "tools" / "hello_pack"
    hello.mkdir(parents=True)
    (hello / "hello_pack.py").write_text("GREETING = 'repo'\n", encoding="utf-8")
    (repo / "note.txt").write_text("REPO\n", encoding="utf-8")
    (repo / "config" / "llm.yaml").write_text("api_key: x\n", encoding="utf-8")
    (repo / ".env").write_text("SECRET=1\n", encoding="utf-8")
    second = tmp_path / "second_project"
    (second / "data").mkdir(parents=True)
    (second / "data" / "report.csv").write_text("col\n1\n", encoding="utf-8")
    ws = tmp_path / "sessions" / "s1"  # 模拟主会话自动生成的空工作空间
    ws.mkdir(parents=True)
    users_dir = tmp_path / "config" / "users"
    (users_dir / "default").mkdir(parents=True)
    (users_dir / "default" / "project_whitelist.yaml").write_text(
        yaml.safe_dump({"entries": [str(repo), str(second)]}), encoding="utf-8"
    )
    monkeypatch.setenv("AGENTOS_CONFIG_ROOT", str(repo / "config"))
    monkeypatch.setenv("AGENTOS_CONFIG_USERS_DIR", str(users_dir))
    repo_anchor.reset_cache()
    yield SimpleNamespace(repo=repo, second=second, ws=ws)
    repo_anchor.reset_cache()


class TestReadWhitelistFallback:
    """相对路径工作空间落空 → 白名单前缀根回退解析一次（裁定第 1 条）。"""

    @pytest.mark.parametrize(
        "relpath",
        [_HELLO_REL, "note.txt"],
    )
    async def test_repo_relative_path_from_empty_session_workspace(
        self, whitelist_env: SimpleNamespace, relpath: str
    ) -> None:
        """R171 主场景：空会话工作空间读仓库相对路径成功，file 回宿主绝对路径。"""
        result = await file_read(path=relpath, workspace=str(whitelist_env.ws))

        assert result.success is True, result.error
        assert "GREETING" in result.output["content"] or "REPO" in result.output["content"]
        expected = (whitelist_env.repo / relpath).resolve()
        assert Path(result.output["file"]) == expected

    async def test_list_directory_repo_relative_from_empty_session_workspace(
        self, whitelist_env: SimpleNamespace
    ) -> None:
        """list_directory 同款回退：仓库相对目录可列出。"""
        result = await list_directory(
            "plugins/shared/tools/hello_pack", workspace=str(whitelist_env.ws)
        )

        assert result.success is True, result.error
        names = {item["name"] for item in result.output["items"]}
        assert names == {"hello_pack.py"}

    async def test_relative_path_missing_everywhere_reports_file_not_found(
        self, whitelist_env: SimpleNamespace
    ) -> None:
        """回退全落空 → 维持原 File not found（不虚构内容）。"""
        result = await file_read(path="ghost.txt", workspace=str(whitelist_env.ws))

        assert result.success is False
        assert "File not found" in result.error

    async def test_relative_fallback_into_second_prefix(
        self, whitelist_env: SimpleNamespace
    ) -> None:
        """相对路径只存在于第二前缀下：回退遍历多前缀命中。"""
        result = await file_read(path="data/report.csv", workspace=str(whitelist_env.ws))

        assert result.success is True, result.error
        assert "col" in result.output["content"]

    async def test_repo_root_fallback_without_whitelist_entry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """白名单未登记仓库时相对回退仍走仓库根（与绝对路径同享仓库读面）。"""
        repo = tmp_path / "repo_nowl"
        (repo / "config" / "kernel").mkdir(parents=True)
        (repo / "src" / "lib.py").mkdir(parents=True)
        (repo / "src" / "lib.py" / "mod.py").write_text("NOWL = 1\n", encoding="utf-8")
        ws = tmp_path / "ws_nowl"
        ws.mkdir(parents=True)
        users_dir = tmp_path / "config_users_nowl"
        (users_dir / "default").mkdir(parents=True)
        (users_dir / "default" / "project_whitelist.yaml").write_text(
            "entries: []\n", encoding="utf-8"
        )
        monkeypatch.setenv("AGENTOS_CONFIG_ROOT", str(repo / "config"))
        monkeypatch.setenv("AGENTOS_CONFIG_USERS_DIR", str(users_dir))
        repo_anchor.reset_cache()
        try:
            result = await file_read(path="src/lib.py/mod.py", workspace=str(ws))
            assert result.success is True, result.error
            assert "NOWL" in result.output["content"]
        finally:
            repo_anchor.reset_cache()

    async def test_repo_denied_dir_fallback_denied_without_whitelist(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """仓库根回退不豁免拒绝目录：config/ 相对路径仍拒（空白名单布局）。"""
        repo = tmp_path / "repo_nowl2"
        (repo / "config" / "kernel").mkdir(parents=True)
        (repo / "config" / "llm.yaml").write_text("api_key: x\n", encoding="utf-8")
        ws = tmp_path / "ws_nowl2"
        ws.mkdir(parents=True)
        users_dir = tmp_path / "config_users_nowl2"
        (users_dir / "default").mkdir(parents=True)
        monkeypatch.setenv("AGENTOS_CONFIG_ROOT", str(repo / "config"))
        monkeypatch.setenv("AGENTOS_CONFIG_USERS_DIR", str(users_dir))
        repo_anchor.reset_cache()
        try:
            result = await file_read(path="config/llm.yaml", workspace=str(ws))
            assert result.success is False
            assert "运行时/产物区" in result.error
        finally:
            repo_anchor.reset_cache()

    async def test_traversal_escape_not_rescued_by_fallback(
        self, tmp_path: Path, whitelist_env: SimpleNamespace
    ) -> None:
        """``..`` 逃逸出工作空间且在白名单外：仍拒绝（回退不开越界口子）。"""
        leaked = tmp_path / "leaked.txt"
        leaked.write_text("x\n", encoding="utf-8")

        result = await file_read(path="../leaked.txt", workspace=str(whitelist_env.ws))

        assert result.success is False
        assert "超出 workspace/project_root" in result.error


class TestWhitelistAbsoluteRead:
    """工作空间外、白名单前缀内的纯读放行；白名单外仍拒绝（裁定第 2 条）。"""

    async def test_second_prefix_absolute_readable(
        self, whitelist_env: SimpleNamespace
    ) -> None:
        """仓库锚之外的第二白名单前缀：绝对路径可读（白名单独立于仓库锚生效）。"""
        target = whitelist_env.second / "data" / "report.csv"

        result = await file_read(path=str(target), workspace=str(whitelist_env.ws))

        assert result.success is True, result.error
        assert "col" in result.output["content"]

    async def test_outside_whitelist_rejected(
        self, tmp_path: Path, whitelist_env: SimpleNamespace
    ) -> None:
        """白名单外路径仍拒绝（读放宽不等于任意读）。"""
        outsider = tmp_path / "outsider"
        outsider.mkdir()
        (outsider / "s.txt").write_text("x\n", encoding="utf-8")

        result = await file_read(path=str(outsider / "s.txt"), workspace=str(whitelist_env.ws))

        assert result.success is False
        assert "超出 workspace/project_root" in result.error

    async def test_repo_denied_dir_via_fallback_rejected(
        self, whitelist_env: SimpleNamespace
    ) -> None:
        """回退候选落在仓库拒绝目录（config/）：拒绝，白名单不淹没仓库锚。"""
        result = await file_read(path="config/llm.yaml", workspace=str(whitelist_env.ws))

        assert result.success is False
        assert "运行时/产物区" in result.error

    async def test_env_via_relative_denied(self, whitelist_env: SimpleNamespace) -> None:
        """凭据黑名单恒先行：仓库根 .env 经相对路径回退仍拒绝。"""
        result = await file_read(path=".env", workspace=str(whitelist_env.ws))

        assert result.success is False
        assert "凭据类文件" in result.error


class TestWorkspacePrecedence:
    """工作空间内已存在文件优先，回退不遮蔽（裁定第 5 条）。"""

    async def test_existing_workspace_file_not_shadowed_by_repo(
        self, whitelist_env: SimpleNamespace
    ) -> None:
        """同名文件工作空间内存在 → 读工作空间副本，不回退仓库。"""
        (whitelist_env.ws / "note.txt").write_text("WS\n", encoding="utf-8")

        result = await file_read(path="note.txt", workspace=str(whitelist_env.ws))

        assert result.success is True, result.error
        assert result.output["content"] == "WS\n"


class TestWhitelistModuleDegradation:
    """project_registry 不可用：白名单降级为空（范围收缩），工具面不断。"""

    async def test_import_failure_degrades_to_empty_whitelist(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """共享根缺失（导入失败）→ 空白名单；工作空间内读取不受影响。"""
        from agentos_builtin_tools import fs_tools

        ws = tmp_path / "ws_deg"
        ws.mkdir()
        (ws / "local.txt").write_text("ok\n", encoding="utf-8")
        monkeypatch.setitem(sys.modules, "project_registry", None)

        assert fs_tools._load_registration_whitelist() == []

        result = await file_read(path="local.txt", workspace=str(ws))
        assert result.success is True, result.error
        assert result.output["content"] == "ok\n"


class TestWriteProtectionUnchanged:
    """写/删/move 对白名单内、工作空间外路径维持拒绝（裁定第 4 条）。"""

    async def test_write_repo_absolute_rejected(self, whitelist_env: SimpleNamespace) -> None:
        """写仓库绝对路径（工作空间外、白名单内）拒绝，仓库文件不被改动。

        （相对写路径解析进工作空间属合法工作空间写，不在此列——写保护锚的是
        落点在工作空间外的路径。）
        """
        target = whitelist_env.repo / _HELLO_REL
        before = target.read_text(encoding="utf-8")

        result = await file_write(
            path=str(target), action="append", content="evil", workspace=str(whitelist_env.ws)
        )

        assert result.success is False
        assert "超出 workspace/project_root" in result.error
        assert target.read_text(encoding="utf-8") == before

    async def test_write_second_prefix_absolute_rejected(
        self, whitelist_env: SimpleNamespace
    ) -> None:
        """写第二白名单前缀（工作空间外）拒绝。"""
        target = whitelist_env.second / "data" / "report.csv"
        before = target.read_text(encoding="utf-8")

        result = await file_write(
            path=str(target), action="append", content="evil", workspace=str(whitelist_env.ws)
        )

        assert result.success is False
        assert target.read_text(encoding="utf-8") == before

    async def test_delete_repo_file_rejected(self, whitelist_env: SimpleNamespace) -> None:
        """删仓库绝对路径（工作空间外）拒绝，文件保留。"""
        target = whitelist_env.repo / "note.txt"

        result = await delete_file(path=str(target), workspace=str(whitelist_env.ws))

        assert result.success is False
        assert "超出 workspace/project_root" in result.error
        assert target.exists()

    async def test_move_into_repo_rejected(self, whitelist_env: SimpleNamespace) -> None:
        """移动目标在仓库内拒绝（move 双端同规）。"""
        src = whitelist_env.ws / "local.txt"
        src.write_text("x\n", encoding="utf-8")

        result = await move_file(
            source=str(src),
            destination=str(whitelist_env.repo / "note.txt"),
            workspace=str(whitelist_env.ws),
        )

        assert result.success is False
        assert src.exists()


class TestSearchWhitelistFallback:
    """enhanced_search 同类只读操作同规（共用单点判定）。"""

    async def test_search_repo_relative_from_empty_session_workspace(
        self, whitelist_env: SimpleNamespace
    ) -> None:
        """搜索相对路径落空 → 回退仓库目录命中源码文件。"""
        result = await enhanced_search(
            query="GREETING",
            path="plugins/shared/tools/hello_pack",
            workspace=str(whitelist_env.ws),
        )

        assert result.success is True, result.error
        assert len(result.output["results"]) == 1

    async def test_search_outside_whitelist_rejected(
        self, tmp_path: Path, whitelist_env: SimpleNamespace
    ) -> None:
        """搜索起点在白名单外仍拒绝。"""
        outsider = tmp_path / "outsider"
        outsider.mkdir()
        (outsider / "s.txt").write_text("GREETING\n", encoding="utf-8")

        result = await enhanced_search(
            query="GREETING", path=str(outsider), workspace=str(whitelist_env.ws)
        )

        assert result.success is False
        assert "超出 workspace/project_root" in result.error
