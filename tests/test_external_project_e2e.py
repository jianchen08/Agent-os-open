"""场景 B E2E 测试 — 无 git 的现有项目完整闭环验证。

测试场景：
  1. 在 tmp_path 下创建一个临时项目目录（无 .git）
  2. 放几个 Python 文件模拟现有项目（如 app.py, utils.py）
  3. 系统自动 git init + 初始 commit
  4. 创建 worktree 隔离
  5. Agent 在 worktree 中修改文件
  6. 评估通过 → git merge → 主仓库 main
  7. cleanup 删除 worktree

验证点：
  - 修改后的文件在原始目录中存在（合并成功）
  - worktree 已清理
  - 原始目录有 .git（自动初始化的）
  - git log 中有初始 commit 和合并记录
"""

from __future__ import annotations

import subprocess
from pathlib import Path


from tools.builtin.resource_merge import ResourceMergeTool

# ── 测试常量 ──────────────────────────────────────────────────────────────

# 初始 app.py 内容
APP_PY_INITIAL = '''\
"""应用程序主模块"""


def main():
    """主入口函数"""
    print("Hello, World!")


if __name__ == "__main__":
    main()
'''

# 初始 utils.py 内容
UTILS_PY_INITIAL = '''\
"""工具函数模块"""


def format_message(name: str) -> str:
    """格式化消息"""
    return f"Hello, {name}!"
'''

# Agent 修改后的 app.py 内容（添加 health_check 函数）
APP_PY_MODIFIED = '''\
"""应用程序主模块"""


def main():
    """主入口函数"""
    print("Hello, World!")


def health_check() -> dict:
    """健康检查接口

    Returns:
        包含服务状态的字典
    """
    return {"status": "ok", "version": "1.0.0"}


if __name__ == "__main__":
    main()
'''

# Git 用户配置（测试用）
_GIT_USER_EMAIL = "test-e2e@agent.local"
_GIT_USER_NAME = "E2E Test"


# ── 辅助函数 ──────────────────────────────────────────────────────────────


def _run_git(*args: str, cwd: Path) -> subprocess.CompletedResult[str]:
    """执行 git 命令并返回结果

    使用 UTF-8 编码避免 Windows 下 GBK 解码中文 commit 消息失败。

    Args:
        *args: git 命令参数
        cwd: 工作目录

    Returns:
        subprocess.CompletedResult 对象

    Raises:
        AssertionError: 命令执行失败时
    """
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    assert result.returncode == 0, (
        f"git {' '.join(args)} 失败: {result.stderr}"
    )
    return result


def _create_temp_project(project_dir: Path) -> None:
    """在指定目录下创建模拟的现有项目文件

    创建 app.py 和 utils.py 两个 Python 文件，
    模拟一个没有任何版本控制的真实项目。

    Args:
        project_dir: 项目根目录路径
    """
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "app.py").write_text(APP_PY_INITIAL, encoding="utf-8")
    (project_dir / "utils.py").write_text(UTILS_PY_INITIAL, encoding="utf-8")


def _init_git_repo(project_dir: Path) -> str:
    """初始化 git 仓库并创建初始 commit

    模拟系统对无 git 项目自动执行的操作：
    1. git init
    2. 配置用户信息
    3. git add -A
    4. git commit

    Args:
        project_dir: 项目根目录路径

    Returns:
        初始 commit 的 hash 值
    """
    _run_git("init", cwd=project_dir)
    _run_git("config", "user.email", _GIT_USER_EMAIL, cwd=project_dir)
    _run_git("config", "user.name", _GIT_USER_NAME, cwd=project_dir)
    _run_git("add", "-A", cwd=project_dir)
    _run_git("commit", "-m", "chore: 初始项目结构", cwd=project_dir)

    result = _run_git("rev-parse", "HEAD", cwd=project_dir)
    return result.stdout.strip()


# ── 测试类 ────────────────────────────────────────────────────────────────


class TestExternalProjectE2E:
    """场景 B：无 git 的现有项目 — 完整闭环 E2E 测试"""

    async def test_external_project_full_closed_loop(self, tmp_path: Path) -> None:
        """测试无 git 现有项目的完整闭环流程

        流程步骤：
        1. 准备：创建无 .git 的临时项目
        2. 自动初始化：git init + 初始 commit
        3. 隔离：创建 worktree
        4. 修改：在 worktree 中修改 app.py
        5. 合并：git merge 将变更合并回主仓库
        6. 清理：删除 worktree 和分支

        验证点：
        - 修改后的 health_check 函数存在于主仓库的 app.py 中
        - worktree 目录已不存在
        - 主仓库存在 .git 目录
        - git log 包含初始 commit 和合并 commit
        """
        # ── 步骤 1：创建无 .git 的临时项目 ──
        project_dir = tmp_path / "my_project"
        _create_temp_project(project_dir)

        # 验证：初始状态下无 .git 目录
        assert not (project_dir / ".git").exists(), "项目初始状态不应有 .git 目录"

        # 验证：初始文件内容正确
        app_content = (project_dir / "app.py").read_text(encoding="utf-8")
        assert "health_check" not in app_content, "初始 app.py 不应包含 health_check"

        # ── 步骤 2：模拟系统自动 git init + 初始 commit ──
        initial_commit = _init_git_repo(project_dir)

        # 验证：自动初始化后存在 .git 目录
        assert (project_dir / ".git").exists(), "自动初始化后应有 .git 目录"

        # 验证：初始 commit 存在
        assert len(initial_commit) >= 7, f"初始 commit hash 有效: {initial_commit}"

        # ── 步骤 3：创建 ResourceMergeTool 并 prepare worktree ──
        tool = ResourceMergeTool(base_path=str(project_dir))
        workspace_dir = tmp_path / "workspaces" / "task_health_check"

        prepare_result = await tool.execute({
            "action": "prepare",
            "workspace": str(workspace_dir),
        })

        # 验证：prepare 成功
        assert prepare_result.success is True, f"prepare 应成功: {prepare_result.error}"
        assert prepare_result.output["action"] == "prepare"
        assert "branch_name" in prepare_result.output
        branch_name = prepare_result.output["branch_name"]
        assert branch_name.startswith("task/"), f"分支名应以 task/ 开头: {branch_name}"

        # 验证：worktree 目录已创建
        assert workspace_dir.exists(), "worktree 目录应已创建"

        # 验证：worktree 中有完整项目文件副本
        assert (workspace_dir / "app.py").exists(), "worktree 中应有 app.py"
        assert (workspace_dir / "utils.py").exists(), "worktree 中应有 utils.py"

        # ── 步骤 4：Agent 在 worktree 中修改 app.py ──
        (workspace_dir / "app.py").write_text(APP_PY_MODIFIED, encoding="utf-8")

        # 验证：worktree 中的修改已生效
        modified_content = (workspace_dir / "app.py").read_text(encoding="utf-8")
        assert "health_check" in modified_content, "worktree 中 app.py 应包含 health_check"

        # ── 步骤 5：通过 git merge 策略将变更合并回主仓库 ──
        merge_result = await tool.execute({
            "action": "merge",
            "workspace": str(workspace_dir),
            "merge_strategy": "git_merge_no_ff",
        })

        # 验证：merge 成功
        assert merge_result.success is True, f"merge 应成功: {merge_result.error}"
        assert merge_result.output["action"] == "merge"
        assert merge_result.output["merge_strategy"] == "git_merge_no_ff"
        assert "merge_commit" in merge_result.output

        # 验证：主仓库的 app.py 已包含 health_check 函数（合并成功）
        merged_app = (project_dir / "app.py").read_text(encoding="utf-8")
        assert "health_check" in merged_app, (
            "合并后主仓库 app.py 应包含 health_check 函数"
        )
        assert "version" in merged_app, (
            "合并后主仓库 app.py 应包含 health_check 的返回字段"
        )

        # 验证：utils.py 未被意外修改
        merged_utils = (project_dir / "utils.py").read_text(encoding="utf-8")
        assert merged_utils == UTILS_PY_INITIAL, (
            "utils.py 内容应保持不变"
        )

        # ── 步骤 6：cleanup 删除 worktree ──
        cleanup_result = await tool.execute({
            "action": "cleanup",
            "workspace": str(workspace_dir),
        })

        # 验证：cleanup 成功
        assert cleanup_result.success is True, f"cleanup 应成功: {cleanup_result.error}"
        assert cleanup_result.output["action"] == "cleanup"
        assert cleanup_result.output["branch_name"] == branch_name

        # ── 最终验证 ──

        # 验证点 1：修改后的文件在原始目录中存在（合并成功）
        assert (project_dir / "app.py").exists(), "合并后 app.py 应存在"
        assert (project_dir / "utils.py").exists(), "合并后 utils.py 应存在"
        final_app = (project_dir / "app.py").read_text(encoding="utf-8")
        assert "def health_check" in final_app, (
            "最终 app.py 应包含 health_check 函数定义"
        )

        # 验证点 2：worktree 已清理
        assert not workspace_dir.exists(), (
            f"worktree 目录应已清理: {workspace_dir}"
        )

        # 验证点 3：原始目录有 .git（自动初始化的）
        assert (project_dir / ".git").exists(), "原始目录应有 .git 目录"

        # 验证点 4：git log 中有初始 commit 和合并记录
        log_result = _run_git(
            "log", "--oneline", "--format=%s",
            cwd=project_dir,
        )
        log_messages = log_result.stdout.strip().splitlines()

        # 检查初始 commit
        assert any("初始项目结构" in msg for msg in log_messages), (
            f"git log 应包含初始 commit，实际: {log_messages}"
        )

        # 检查合并 commit（git merge_no_ff 会产生 merge commit）
        # 合并 commit 消息格式可能是 "Merge branch 'task/xxx'"
        # 或 auto commit 消息 "auto: merge from task/xxx"
        has_merge_record = any(
            "merge" in msg.lower() or "task/" in msg.lower()
            for msg in log_messages
        )
        assert has_merge_record, (
            f"git log 应包含合并记录，实际: {log_messages}"
        )

        # 验证至少有 2 条 commit（初始 + 合并相关）
        assert len(log_messages) >= 2, (
            f"应至少有 2 条 commit 记录（初始 + 合并），实际 {len(log_messages)} 条: {log_messages}"
        )

        # 验证分支已清理（task/ 开头的分支不应存在）
        branch_result = _run_git("branch", "--list", "task/*", cwd=project_dir)
        assert branch_result.stdout.strip() == "", (
            f"task/ 开头的分支应已删除，实际存在: {branch_result.stdout.strip()}"
        )

    async def test_external_project_prepare_idempotent(self, tmp_path: Path) -> None:
        """测试对已创建的 worktree 重复执行 prepare 不会报错

        验证 prepare 操作的幂等性：对已存在的 worktree 再次调用 prepare
        应返回成功并提示"无需重复创建"。
        """
        # 准备：创建项目并初始化 git
        project_dir = tmp_path / "idempotent_project"
        _create_temp_project(project_dir)
        _init_git_repo(project_dir)

        tool = ResourceMergeTool(base_path=str(project_dir))
        workspace_dir = tmp_path / "ws_idempotent"

        # 第一次 prepare
        result1 = await tool.execute({
            "action": "prepare",
            "workspace": str(workspace_dir),
        })
        assert result1.success is True

        # 第二次 prepare（幂等性验证）
        result2 = await tool.execute({
            "action": "prepare",
            "workspace": str(workspace_dir),
        })
        assert result2.success is True
        assert "无需重复创建" in result2.output["message"]

        # 清理
        await tool.execute({
            "action": "cleanup",
            "workspace": str(workspace_dir),
        })

    async def test_external_project_rollback_before_merge(self, tmp_path: Path) -> None:
        """测试在 worktree 中修改文件后执行 rollback，变更被丢弃

        验证 rollback 操作能正确恢复 worktree 到 prepare 时的状态。
        """
        # 准备：创建项目并初始化 git
        project_dir = tmp_path / "rollback_project"
        _create_temp_project(project_dir)
        _init_git_repo(project_dir)

        tool = ResourceMergeTool(base_path=str(project_dir))
        workspace_dir = tmp_path / "ws_rollback"

        # prepare
        prepare_result = await tool.execute({
            "action": "prepare",
            "workspace": str(workspace_dir),
        })
        assert prepare_result.success is True

        # 在 worktree 中修改 app.py
        (workspace_dir / "app.py").write_text(
            "# 被篡改的内容\n", encoding="utf-8"
        )

        # rollback
        rollback_result = await tool.execute({
            "action": "rollback",
            "workspace": str(workspace_dir),
        })
        assert rollback_result.success is True

        # 验证：worktree 中的 app.py 恢复到原始内容
        restored_content = (workspace_dir / "app.py").read_text(encoding="utf-8")
        assert restored_content == APP_PY_INITIAL, (
            "rollback 后 app.py 应恢复到初始内容"
        )

        # 清理
        await tool.execute({
            "action": "cleanup",
            "workspace": str(workspace_dir),
        })

    async def test_external_project_git_status_tracks_changes(self, tmp_path: Path) -> None:
        """测试 worktree 中修改文件后 git_status 能正确追踪变更

        验证 git_status 操作能准确反映 worktree 中的文件修改状态。
        """
        # 准备：创建项目并初始化 git
        project_dir = tmp_path / "status_project"
        _create_temp_project(project_dir)
        _init_git_repo(project_dir)

        tool = ResourceMergeTool(base_path=str(project_dir))
        workspace_dir = tmp_path / "ws_status"

        # prepare
        await tool.execute({
            "action": "prepare",
            "workspace": str(workspace_dir),
        })

        # 修改前：git_status 应为空（无变更）
        status_before = await tool.execute({
            "action": "git_status",
            "workspace": str(workspace_dir),
        })
        assert status_before.success is True
        assert status_before.output["total_changes"] == 0, (
            "修改前不应有变更"
        )

        # 修改 app.py
        (workspace_dir / "app.py").write_text(APP_PY_MODIFIED, encoding="utf-8")

        # 修改后：git_status 应检测到变更
        status_after = await tool.execute({
            "action": "git_status",
            "workspace": str(workspace_dir),
        })
        assert status_after.success is True
        assert status_after.output["total_changes"] > 0, (
            "修改后应检测到变更"
        )

        # 清理
        await tool.execute({
            "action": "cleanup",
            "workspace": str(workspace_dir),
        })

    async def test_external_project_git_log_in_worktree(self, tmp_path: Path) -> None:
        """测试 worktree 中的 git_log 能查看从主仓库继承的提交历史

        验证 worktree 能看到主仓库的初始 commit。
        """
        # 准备：创建项目并初始化 git
        project_dir = tmp_path / "log_project"
        _create_temp_project(project_dir)
        _init_git_repo(project_dir)

        tool = ResourceMergeTool(base_path=str(project_dir))
        workspace_dir = tmp_path / "ws_log"

        # prepare
        await tool.execute({
            "action": "prepare",
            "workspace": str(workspace_dir),
        })

        # git_log
        log_result = await tool.execute({
            "action": "git_log",
            "workspace": str(workspace_dir),
        })
        assert log_result.success is True
        assert log_result.output["count"] >= 1, (
            "worktree 应至少有一条 commit 记录（继承自主仓库）"
        )

        # 验证初始 commit 在历史中
        commit_messages = [
            c["message"] for c in log_result.output["commits"]
        ]
        assert any("初始项目结构" in msg for msg in commit_messages), (
            f"worktree 的 git log 应包含初始 commit，实际: {commit_messages}"
        )

        # 清理
        await tool.execute({
            "action": "cleanup",
            "workspace": str(workspace_dir),
        })

    async def test_external_project_git_diff_shows_changes(self, tmp_path: Path) -> None:
        """测试 worktree 中修改文件后 git_diff 能显示具体差异

        验证 git_diff 能准确展示 worktree 中的文件变更内容。
        """
        # 准备：创建项目并初始化 git
        project_dir = tmp_path / "diff_project"
        _create_temp_project(project_dir)
        _init_git_repo(project_dir)

        tool = ResourceMergeTool(base_path=str(project_dir))
        workspace_dir = tmp_path / "ws_diff"

        # prepare
        await tool.execute({
            "action": "prepare",
            "workspace": str(workspace_dir),
        })

        # 修改 app.py
        (workspace_dir / "app.py").write_text(APP_PY_MODIFIED, encoding="utf-8")

        # git_diff
        diff_result = await tool.execute({
            "action": "git_diff",
            "workspace": str(workspace_dir),
        })
        assert diff_result.success is True
        diff_content = diff_result.output["diff"]
        assert "health_check" in diff_content, (
            "git diff 应包含 health_check 相关变更"
        )

        # 清理
        await tool.execute({
            "action": "cleanup",
            "workspace": str(workspace_dir),
        })

    async def test_external_project_copy_merge_strategy(self, tmp_path: Path) -> None:
        """测试使用 copy 合并策略将 worktree 变更复制到主仓库

        验证 copy 策略的合并流程：通过文件复制方式将变更合并到目标目录。
        """
        # 准备：创建项目并初始化 git
        project_dir = tmp_path / "copy_project"
        _create_temp_project(project_dir)
        _init_git_repo(project_dir)

        tool = ResourceMergeTool(base_path=str(project_dir))
        workspace_dir = tmp_path / "ws_copy"

        # prepare
        await tool.execute({
            "action": "prepare",
            "workspace": str(workspace_dir),
        })

        # 在 worktree 中修改 app.py
        (workspace_dir / "app.py").write_text(APP_PY_MODIFIED, encoding="utf-8")

        # 使用 copy 策略合并
        merge_result = await tool.execute({
            "action": "merge",
            "workspace": str(workspace_dir),
            "merge_strategy": "copy",
        })
        assert merge_result.success is True
        assert merge_result.output["mode"] == "worktree"
        assert "app.py" in merge_result.output["merged_files"], (
            "app.py 应在合并文件列表中"
        )
        assert "app.py" in merge_result.output["change_report"]["modified"], (
            "app.py 应在修改文件报告中"
        )

        # 验证主仓库的 app.py 已更新
        merged_app = (project_dir / "app.py").read_text(encoding="utf-8")
        assert "health_check" in merged_app, (
            "copy 合并后 app.py 应包含 health_check"
        )

        # 清理
        await tool.execute({
            "action": "cleanup",
            "workspace": str(workspace_dir),
        })

    async def test_external_project_new_file_merge(self, tmp_path: Path) -> None:
        """测试 worktree 中新增文件后通过 git_merge 合并到主仓库

        验证新增文件（而非修改已有文件）的合并流程。
        """
        # 准备：创建项目并初始化 git
        project_dir = tmp_path / "newfile_project"
        _create_temp_project(project_dir)
        _init_git_repo(project_dir)

        tool = ResourceMergeTool(base_path=str(project_dir))
        workspace_dir = tmp_path / "ws_newfile"

        # prepare
        await tool.execute({
            "action": "prepare",
            "workspace": str(workspace_dir),
        })

        # 在 worktree 中新增 config.py
        config_content = '''\
"""配置模块"""


APP_NAME = "MyApp"
VERSION = "1.0.0"
DEBUG = False
'''
        (workspace_dir / "config.py").write_text(config_content, encoding="utf-8")

        # git merge
        merge_result = await tool.execute({
            "action": "merge",
            "workspace": str(workspace_dir),
            "merge_strategy": "git_merge_no_ff",
        })
        assert merge_result.success is True

        # 验证新文件已合并到主仓库
        assert (project_dir / "config.py").exists(), (
            "合并后 config.py 应存在于主仓库"
        )
        merged_config = (project_dir / "config.py").read_text(encoding="utf-8")
        assert "APP_NAME" in merged_config, "config.py 内容应正确"

        # 清理
        await tool.execute({
            "action": "cleanup",
            "workspace": str(workspace_dir),
        })

    async def test_external_project_git_commit_in_worktree(self, tmp_path: Path) -> None:
        """测试在 worktree 中执行 git_commit 提交变更

        验证 git_commit 操作能在 worktree 中正确提交文件变更。
        """
        # 准备：创建项目并初始化 git
        project_dir = tmp_path / "commit_project"
        _create_temp_project(project_dir)
        _init_git_repo(project_dir)

        tool = ResourceMergeTool(base_path=str(project_dir))
        workspace_dir = tmp_path / "ws_commit"

        # prepare
        await tool.execute({
            "action": "prepare",
            "workspace": str(workspace_dir),
        })

        # 修改 app.py
        (workspace_dir / "app.py").write_text(APP_PY_MODIFIED, encoding="utf-8")

        # git_commit
        commit_result = await tool.execute({
            "action": "git_commit",
            "workspace": str(workspace_dir),
            "message": "feat: 添加 health_check 函数",
        })
        assert commit_result.success is True
        assert commit_result.output["commit_hash"] is not None, (
            "应返回 commit hash"
        )

        # 验证 git_log 中能看到新提交
        log_result = await tool.execute({
            "action": "git_log",
            "workspace": str(workspace_dir),
        })
        assert log_result.success is True
        commit_messages = [c["message"] for c in log_result.output["commits"]]
        assert any("health_check" in msg for msg in commit_messages), (
            f"git log 应包含 health_check 相关提交，实际: {commit_messages}"
        )

        # 清理
        await tool.execute({
            "action": "cleanup",
            "workspace": str(workspace_dir),
        })
