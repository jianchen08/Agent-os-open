# @feature: FP-0.2.spill_guard 内置工具读护栏 | @ci: python-coverage
"""区域读写判定测试（ADR 2026-09-24-read-deny-write-zones：读黑名单制+写区白名单）。

锁定契约（file_read / list_directory / enhanced_search / 写删move 共用
fs_tools._check_workspace_path 单点判定）：
1. 读黑名单制：根外普通路径全放；凭据黑名单、仓库拒绝集、read_deny 前缀
   除外（读不再需要名单/授权）；
2. 相对路径以工作空间解析落空 → 按白名单前缀根/仓库根回退解析一次（R171
   主场景：L1 会话空工作空间读仓库相对路径不再 File not found）；
3. 写区：entries 前缀内写/删/move 放行（交上层按权限档走），仓库自保
   目录对写恒拒（系统自保优先于写区）；
4. 写区外拒绝并提示授权通道（名单/授权卡）；
5. read_deny 只挡读不挡写（读写语义按节分离，不按方向双名单）；
6. 工作空间内已存在文件优先（回退只在落空时发生）。
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
def zone_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """假仓库（repo 锚标记）+ 第二写区 + read_deny 排除区 + 空会话工作空间。

    名单经 env AGENTOS_CONFIG_USERS_DIR 钉到 tmp（与登记侧测试同款隔离）；
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
    denied = tmp_path / "private_zone"
    denied.mkdir(parents=True)
    (denied / "diary.txt").write_text("PRIVATE\n", encoding="utf-8")
    ws = tmp_path / "sessions" / "s1"  # 模拟主会话自动生成的空工作空间
    ws.mkdir(parents=True)
    users_dir = tmp_path / "config" / "users"
    (users_dir / "default").mkdir(parents=True)
    (users_dir / "default" / "project_whitelist.yaml").write_text(
        yaml.safe_dump(
            {
                "entries": [str(repo), str(second)],
                "read_deny": [str(denied)],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENTOS_CONFIG_ROOT", str(repo / "config"))
    monkeypatch.setenv("AGENTOS_CONFIG_USERS_DIR", str(users_dir))
    repo_anchor.reset_cache()
    yield SimpleNamespace(repo=repo, second=second, denied=denied, ws=ws)
    repo_anchor.reset_cache()


class TestReadDenylist:
    """读黑名单制（决策1）：根外普通路径全放，凭据/仓库拒绝集/read_deny 除外。"""

    @pytest.mark.parametrize(
        "relpath",
        [_HELLO_REL, "note.txt"],
    )
    async def test_repo_relative_path_from_empty_session_workspace(
        self, zone_env: SimpleNamespace, relpath: str
    ) -> None:
        """R171 主场景：空会话工作空间读仓库相对路径成功，file 回宿主绝对路径。"""
        result = await file_read(path=relpath, workspace=str(zone_env.ws))

        assert result.success is True, result.error
        assert "GREETING" in result.output["content"] or "REPO" in result.output["content"]
        expected = (zone_env.repo / relpath).resolve()
        assert Path(result.output["file"]) == expected

    async def test_list_directory_repo_relative_from_empty_session_workspace(
        self, zone_env: SimpleNamespace
    ) -> None:
        """list_directory 同款回退：仓库相对目录可列出。"""
        result = await list_directory(
            "plugins/shared/tools/hello_pack", workspace=str(zone_env.ws)
        )

        assert result.success is True, result.error
        names = {item["name"] for item in result.output["items"]}
        assert names == {"hello_pack.py"}

    async def test_relative_path_missing_everywhere_reports_file_not_found(
        self, zone_env: SimpleNamespace
    ) -> None:
        """回退全落空 → 维持原 File not found（不虚构内容）。"""
        result = await file_read(path="ghost.txt", workspace=str(zone_env.ws))

        assert result.success is False
        assert "File not found" in result.error

    async def test_relative_fallback_into_second_prefix(
        self, zone_env: SimpleNamespace
    ) -> None:
        """相对路径只存在于第二前缀下：回退遍历多前缀命中。"""
        result = await file_read(path="data/report.csv", workspace=str(zone_env.ws))

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

    async def test_outside_any_prefix_readable(
        self, zone_env: SimpleNamespace, tmp_path: Path
    ) -> None:
        """名单外普通路径读放行（读不再需要名单/授权）。"""
        outsider = tmp_path / "outsider"
        outsider.mkdir()
        (outsider / "s.txt").write_text("x\n", encoding="utf-8")

        result = await file_read(path=str(outsider / "s.txt"), workspace=str(zone_env.ws))

        assert result.success is True, result.error
        assert "x" in result.output["content"]

    async def test_read_deny_prefix_rejected(self, zone_env: SimpleNamespace) -> None:
        """read_deny 前缀命中拒绝读取（用户排除区）。"""
        result = await file_read(
            path=str(zone_env.denied / "diary.txt"), workspace=str(zone_env.ws)
        )

        assert result.success is False
        assert "read_deny" in result.error

    async def test_read_deny_prefix_descendant_rejected(
        self, zone_env: SimpleNamespace
    ) -> None:
        """read_deny 前缀授权任意层级后代（子目录同拒）。"""
        child = zone_env.denied / "sub"
        child.mkdir()
        (child / "x.txt").write_text("y\n", encoding="utf-8")

        result = await file_read(path=str(child / "x.txt"), workspace=str(zone_env.ws))

        assert result.success is False
        assert "read_deny" in result.error

    async def test_repo_denied_dir_via_fallback_rejected(
        self, zone_env: SimpleNamespace
    ) -> None:
        """回退候选落在仓库拒绝目录（config/）：拒绝（仓库锚先行于 read_deny 链）。"""
        result = await file_read(path="config/llm.yaml", workspace=str(zone_env.ws))

        assert result.success is False
        assert "运行时/产物区" in result.error

    async def test_env_via_relative_denied(self, zone_env: SimpleNamespace) -> None:
        """凭据黑名单恒先行：仓库根 .env 经相对路径回退仍拒绝。"""
        result = await file_read(path=".env", workspace=str(zone_env.ws))

        assert result.success is False
        assert "凭据类文件" in result.error

    async def test_traversal_escape_readable_under_denylist(
        self, zone_env: SimpleNamespace
    ) -> None:
        """``..`` 逃逸出工作空间的普通文件读放行（区域不设限；凭据/排除区仍拦）。"""
        leaked = zone_env.ws.parent / "leaked.txt"
        leaked.write_text("x\n", encoding="utf-8")

        result = await file_read(path="../leaked.txt", workspace=str(zone_env.ws))

        assert result.success is True, result.error
        assert "x" in result.output["content"]


class TestWorkspacePrecedence:
    """工作空间内已存在文件优先，回退不遮蔽（契约第 6 条）。"""

    async def test_existing_workspace_file_not_shadowed_by_repo(
        self, zone_env: SimpleNamespace
    ) -> None:
        """同名文件工作空间内存在 → 读工作空间副本，不回退仓库。"""
        (zone_env.ws / "note.txt").write_text("WS\n", encoding="utf-8")

        result = await file_read(path="note.txt", workspace=str(zone_env.ws))

        assert result.success is True, result.error
        assert result.output["content"] == "WS\n"


class TestWhitelistModuleDegradation:
    """project_registry 不可用：写区/读排除名单双双降级为空（范围收缩），工具面不断。"""

    async def test_import_failure_degrades_to_empty_whitelist(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """共享根缺失（导入失败）→ 空名单；工作空间内读取不受影响。"""
        from agentos_builtin_tools import fs_tools

        ws = tmp_path / "ws_deg"
        ws.mkdir()
        (ws / "local.txt").write_text("ok\n", encoding="utf-8")
        monkeypatch.setitem(sys.modules, "project_registry", None)

        assert fs_tools._load_registration_whitelist() == []
        # 判定单源 zone_policy 的读排除加载同款降级（fs_tools 判定链经它）
        assert fs_tools._zone_policy._load_read_deny() == []

        result = await file_read(path="local.txt", workspace=str(ws))
        assert result.success is True, result.error
        assert result.output["content"] == "ok\n"


class TestWriteZones:
    """写区语义（决策2/3）：entries 前缀写放行；区外拒绝；仓库自保目录恒拒。"""

    async def test_write_repo_source_zone_allowed(self, zone_env: SimpleNamespace) -> None:
        """仓库根在名单内：源码区写放行（写区语义，交上层按权限档走）。"""
        target = zone_env.repo / _HELLO_REL

        result = await file_write(
            path=str(target),
            action="append",
            content="\n# touched",
            workspace=str(zone_env.ws),
        )

        assert result.success is True, result.error
        assert "# touched" in target.read_text(encoding="utf-8")

    async def test_write_second_prefix_allowed(self, zone_env: SimpleNamespace) -> None:
        """第二写区内追加放行。"""
        target = zone_env.second / "data" / "report.csv"

        result = await file_write(
            path=str(target), action="append", content="2\n", workspace=str(zone_env.ws)
        )

        assert result.success is True, result.error
        assert "2" in target.read_text(encoding="utf-8")

    async def test_delete_in_zone_allowed(self, zone_env: SimpleNamespace) -> None:
        """删写区内文件放行。"""
        victim = zone_env.second / "data" / "report.csv"

        result = await delete_file(path=str(victim), workspace=str(zone_env.ws))

        assert result.success is True, result.error
        assert not victim.exists()

    async def test_move_within_zone_allowed(self, zone_env: SimpleNamespace) -> None:
        """写区内移动放行（双端同区）。"""
        src = zone_env.second / "data" / "report.csv"
        dst = zone_env.second / "data" / "renamed.csv"

        result = await move_file(
            source=str(src), destination=str(dst), workspace=str(zone_env.ws)
        )

        assert result.success is True, result.error
        assert dst.exists()
        assert not src.exists()

    async def test_write_outside_all_zones_rejected(
        self, zone_env: SimpleNamespace, tmp_path: Path
    ) -> None:
        """区外写拒绝并提示授权通道，文件不被创建。"""
        outsider = tmp_path / "outsider2"
        outsider.mkdir()
        target = outsider / "new.txt"

        result = await file_write(
            path=str(target), action="write", content="x", workspace=str(zone_env.ws)
        )

        assert result.success is False
        assert "写区名单" in result.error
        assert "授权" in result.error
        assert not target.exists()

    async def test_write_repo_denied_dir_rejected_even_in_zone(
        self, zone_env: SimpleNamespace
    ) -> None:
        """仓库自保目录对写恒拒：名单覆盖仓库根也不放行 config/（系统自保优先）。"""
        target = zone_env.repo / "config" / "evil.yaml"

        result = await file_write(
            path=str(target), action="write", content="x", workspace=str(zone_env.ws)
        )

        assert result.success is False
        assert "运行时/产物区" in result.error
        assert not target.exists()

    async def test_write_with_pipeline_authorized_zone_allowed(
        self, zone_env: SimpleNamespace
    ) -> None:
        """管道级授权前缀（zone_grant 卡批准注入）放行区外写（决策3）。"""
        import json

        granted = zone_env.ws.parent / "granted"
        granted.mkdir()
        target = granted / "new.txt"

        result = await file_write(
            path=str(target),
            action="write",
            content="x",
            workspace=str(zone_env.ws),
            authorized_zones=json.dumps([str(granted)]),
        )

        assert result.success is True, result.error
        assert target.read_text(encoding="utf-8") == "x"

    async def test_write_with_invalid_authorized_zones_fails_closed(
        self, zone_env: SimpleNamespace, tmp_path: Path
    ) -> None:
        """authorized_zones 非法 JSON：按无授权处理（安全侧），区外写仍拒。"""
        outsider = tmp_path / "outsider4"
        outsider.mkdir()
        target = outsider / "new.txt"

        result = await file_write(
            path=str(target),
            action="write",
            content="x",
            workspace=str(zone_env.ws),
            authorized_zones="not-json",
        )

        assert result.success is False
        assert not target.exists()

    async def test_read_deny_does_not_gate_write_rejection_reason(
        self, zone_env: SimpleNamespace
    ) -> None:
        """read_deny 只挡读不挡写：排除区写拒绝的原因是区外（契约第 5 条）。"""
        target = zone_env.denied / "new.txt"

        result = await file_write(
            path=str(target), action="write", content="x", workspace=str(zone_env.ws)
        )

        assert result.success is False
        assert "只读排除区" not in result.error
        assert "写区名单" in result.error
        assert not target.exists()


class TestSearchZones:
    """enhanced_search 同类只读操作同规（共用单点判定）。"""

    async def test_search_repo_relative_from_empty_session_workspace(
        self, zone_env: SimpleNamespace
    ) -> None:
        """搜索相对路径落空 → 回退仓库目录命中源码文件。"""
        result = await enhanced_search(
            query="GREETING",
            path="plugins/shared/tools/hello_pack",
            workspace=str(zone_env.ws),
        )

        assert result.success is True, result.error
        assert len(result.output["results"]) == 1

    async def test_search_outside_zones_readable(
        self, zone_env: SimpleNamespace, tmp_path: Path
    ) -> None:
        """搜索根外普通目录放行（读黑名单制同规）。"""
        outsider = tmp_path / "outsider3"
        outsider.mkdir()
        (outsider / "s.txt").write_text("GREETING\n", encoding="utf-8")

        result = await enhanced_search(
            query="GREETING", path=str(outsider), workspace=str(zone_env.ws)
        )

        assert result.success is True, result.error
