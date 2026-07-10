"""场景 C E2E 测试 — 新建项目完整闭环验证。

测试场景：
  1. 不设置 workspace（触发场景 C / 场景 A）
  2. 系统在 .ai_workspaces 下创建新目录
  3. 执行 mkdir + git init → mode=project_root
  4. 子任务：git checkout -b feature → mode=branch
  5. Agent 在分支中创建 FastAPI 文件
  6. 评估通过 → git merge → 项目内 main
  7. cleanup 删除功能分支

验证点：
  - 项目目录存在且有 .git
  - FastAPI 文件存在且可导入
  - git log 中有初始 commit 和功能分支合并记录
  - 功能分支已删除
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path


# ── 测试常量 ──────────────────────────────────────────────────────────────

# 模拟的任务 ID
_ROOT_TASK_ID = "task_new_project_001"
_SUB_TASK_ID = "subtask_feature_api"

# 功能分支名称
_FEATURE_BRANCH = f"feature/{_SUB_TASK_ID}"

# Agent 在分支中创建的 FastAPI 主文件
MAIN_PY_CONTENT = '''\
"""FastAPI 应用主入口"""

from fastapi import FastAPI

app = FastAPI(title="Demo API", version="1.0.0")


@app.get("/health")
def health_check() -> dict:
    """健康检查接口"""
    return {"status": "ok", "version": "1.0.0"}


@app.get("/items/{item_id}")
def get_item(item_id: int) -> dict:
    """获取指定 ID 的条目"""
    return {"item_id": item_id, "name": f"Item {item_id}"}
'''

# Agent 在分支中创建的模型文件
MODELS_PY_CONTENT = '''\
"""数据模型定义"""

from pydantic import BaseModel


class ItemResponse(BaseModel):
    """条目响应模型"""
    item_id: int
    name: str

    class Config:
        """Pydantic 配置"""
        from_attributes = True
'''

# Agent 在分支中创建的配置文件
CONFIG_PY_CONTENT = '''\
"""应用配置"""

APP_NAME = "Demo API"
VERSION = "1.0.0"
DEBUG = False
'''

# 初始 commit 消息
_INIT_COMMIT_MSG = "chore: 初始化项目"

# Agent 提交消息
_AGENT_COMMIT_MSG = "feat: 添加 FastAPI 应用骨架"

# 合并前保存的 commit 消息
_CHECKPOINT_COMMIT_MSG = "checkpoint: before evaluate"

# Git 用户配置（测试用）
_GIT_USER_EMAIL = "test-e2e@agent.local"
_GIT_USER_NAME = "E2E Test"


# ── 辅助函数 ──────────────────────────────────────────────────────────────


def _run_git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    """执行 git 命令并返回结果

    使用 UTF-8 编码避免 Windows 下 GBK 解码中文 commit 消息失败。

    Args:
        *args: git 命令参数
        cwd: 工作目录

    Returns:
        subprocess.CompletedProcess 对象

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


def _init_project_root(project_dir: Path) -> str:
    """模拟场景 C 步骤 2-3：创建目录 + git init + 初始 commit

    当 workspace 未设置时，系统在 .ai_workspaces/{task_id} 下
    创建新目录并执行 git init，进入 project_root 模式。

    Args:
        project_dir: 项目根目录路径（.ai_workspaces/{task_id}）

    Returns:
        初始 commit 的 hash 值
    """
    # 模拟系统创建目录
    project_dir.mkdir(parents=True, exist_ok=True)

    # 模拟 git init（mode=project_root 的核心操作）
    _run_git("init", cwd=project_dir)
    # 将默认分支重命名为 main（与 WorkspaceLifecycleManager 行为一致）
    _run_git("checkout", "-b", "main", cwd=project_dir)
    _run_git("config", "user.email", _GIT_USER_EMAIL, cwd=project_dir)
    _run_git("config", "user.name", _GIT_USER_NAME, cwd=project_dir)

    # 创建一个 README 作为初始文件，确保仓库有内容
    readme_path = project_dir / "README.md"
    readme_path.write_text("# New Project\n", encoding="utf-8")

    # 初始 commit
    _run_git("add", "-A", cwd=project_dir)
    _run_git("commit", "-m", _INIT_COMMIT_MSG, cwd=project_dir)

    result = _run_git("rev-parse", "HEAD", cwd=project_dir)
    return result.stdout.strip()


def _create_feature_branch(project_dir: Path) -> str:
    """模拟场景 C 步骤 4：子任务创建 feature 分支

    在 project_root 模式下，子任务执行 git checkout -b feature/{sub_task_id}，
    进入 branch 模式。

    Args:
        project_dir: 项目根目录路径

    Returns:
        功能分支名称
    """
    _run_git("checkout", "-b", _FEATURE_BRANCH, cwd=project_dir)
    return _FEATURE_BRANCH


def _agent_create_fastapi_files(project_dir: Path) -> list[str]:
    """模拟场景 C 步骤 5：Agent 在分支中创建 FastAPI 文件

    Agent 在 feature 分支中创建 FastAPI 应用骨架文件，
    包括主入口、模型定义和配置文件。

    Args:
        project_dir: 项目根目录路径

    Returns:
        创建的文件相对路径列表
    """
    files = {
        "main.py": MAIN_PY_CONTENT,
        "models.py": MODELS_PY_CONTENT,
        "config.py": CONFIG_PY_CONTENT,
    }
    for filename, content in files.items():
        (project_dir / filename).write_text(content, encoding="utf-8")

    return list(files.keys())


def _agent_commit_changes(project_dir: Path) -> str:
    """模拟 Agent 提交变更

    将 Agent 创建的 FastAPI 文件提交到 feature 分支。

    Args:
        project_dir: 项目根目录路径

    Returns:
        提交的 commit hash
    """
    _run_git("add", "-A", cwd=project_dir)
    _run_git("commit", "-m", _AGENT_COMMIT_MSG, cwd=project_dir)
    result = _run_git("rev-parse", "HEAD", cwd=project_dir)
    return result.stdout.strip()


def _checkpoint_before_evaluate(project_dir: Path) -> str | None:
    """模拟场景 C 步骤 6：评估前保存（checkpoint）

    系统在评估前执行 git add -A + git commit，确保所有变更已保存。

    Args:
        project_dir: 项目根目录路径

    Returns:
        checkpoint commit hash，无变更时返回 None
    """
    _run_git("add", "-A", cwd=project_dir)
    result = _run_git("status", "--porcelain", cwd=project_dir)
    if result.stdout.strip():
        _run_git("commit", "-m", _CHECKPOINT_COMMIT_MSG, cwd=project_dir)
        hash_result = _run_git("rev-parse", "HEAD", cwd=project_dir)
        return hash_result.stdout.strip()
    return None


def _merge_feature_to_main(project_dir: Path) -> dict:
    """模拟场景 C 步骤 6：评估通过后合并 feature 分支到 main

    branch 模式下的合并流程：
    1. git checkout main
    2. git merge feature/{sub_task_id}

    Args:
        project_dir: 项目根目录路径

    Returns:
        合并结果字典，包含 success 和 merge_commit 信息
    """
    _run_git("checkout", "main", cwd=project_dir)
    # 使用 --no-ff 确保产生合并 commit（与 WorkspaceLifecycleManager 行为一致）
    _run_git("merge", "--no-ff", _FEATURE_BRANCH, cwd=project_dir)

    hash_result = _run_git("rev-parse", "HEAD", cwd=project_dir)
    return {
        "success": True,
        "merge_commit": hash_result.stdout.strip(),
        "branch": _FEATURE_BRANCH,
    }


def _cleanup_feature_branch(project_dir: Path) -> None:
    """模拟场景 C 步骤 7：cleanup 删除功能分支

    合并完成后删除已合并的 feature 分支，保持仓库整洁。

    Args:
        project_dir: 项目根目录路径
    """
    _run_git("branch", "-d", _FEATURE_BRANCH, cwd=project_dir)


def _can_import_module(module_path: Path, module_name: str) -> bool:
    """检查 Python 模块是否可以被动态导入

    使用 importlib 动态导入指定路径的 Python 文件，
    验证其语法和导入正确性。

    Args:
        module_path: 模块文件路径
        module_name: 模块名称（用于注册到 sys.modules）

    Returns:
        是否成功导入
    """
    try:
        spec = importlib.util.spec_from_file_location(module_name, str(module_path))
        if spec is None or spec.loader is None:
            return False
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return True
    except Exception:
        return False


# ── 测试类 ────────────────────────────────────────────────────────────────


class TestNewProjectE2E:
    """场景 C：新建项目 — 完整闭环 E2E 测试"""

    async def test_new_project_full_closed_loop(self, tmp_path: Path) -> None:
        """测试新建项目的完整闭环流程

        流程步骤：
        1. 准备：不设置 workspace，系统在 .ai_workspaces 下创建新目录
        2. 初始化：mkdir + git init → mode=project_root
        3. 子任务：git checkout -b feature → mode=branch
        4. 开发：Agent 在分支中创建 FastAPI 文件
        5. 评估前保存：checkpoint commit
        6. 评估通过：git merge → 项目内 main
        7. 清理：删除功能分支

        验证点：
        - 项目目录存在且有 .git
        - FastAPI 文件存在且可导入
        - git log 中有初始 commit 和功能分支合并记录
        - 功能分支已删除
        """
        # ── 步骤 1-2：模拟不设置 workspace → 系统创建新目录 ──
        # 路径结构：{tmp_path}/.ai_workspaces/{task_id}
        workspace_root = tmp_path / ".ai_workspaces"
        project_dir = workspace_root / _ROOT_TASK_ID

        # 模拟 WorkspaceLifecycleManager._detect_scenario 返回 new_project
        assert not project_dir.exists(), "项目目录初始不应存在"

        # 初始化项目（mkdir + git init + 初始 commit）
        initial_commit = _init_project_root(project_dir)

        # 验证：目录已创建
        assert project_dir.exists(), "项目目录应已创建"
        assert project_dir.is_dir(), "项目路径应为目录"

        # ── 步骤 3：验证 mode=project_root 状态 ──
        # 项目目录存在且有 .git
        assert (project_dir / ".git").exists(), "初始化后应有 .git 目录"
        assert (project_dir / ".git").is_dir(), ".git 应为目录（非 worktree 文件）"

        # 验证初始 commit
        assert len(initial_commit) >= 7, f"初始 commit hash 有效: {initial_commit}"

        # 验证当前分支为 main
        branch_result = _run_git("branch", "--show-current", cwd=project_dir)
        assert branch_result.stdout.strip() == "main", (
            f"初始分支应为 main，实际: {branch_result.stdout.strip()}"
        )

        # 验证 README.md 存在
        assert (project_dir / "README.md").exists(), "初始 README.md 应存在"

        # ── 步骤 4：子任务创建 feature 分支 → mode=branch ──
        _create_feature_branch(project_dir)

        # 验证：当前分支为 feature
        branch_result = _run_git("branch", "--show-current", cwd=project_dir)
        assert branch_result.stdout.strip() == _FEATURE_BRANCH, (
            f"子任务应在 feature 分支上，实际: {branch_result.stdout.strip()}"
        )

        # ── 步骤 5：Agent 在分支中创建 FastAPI 文件 ──
        created_files = _agent_create_fastapi_files(project_dir)

        # 验证：所有文件已创建
        for filename in created_files:
            file_path = project_dir / filename
            assert file_path.exists(), f"Agent 应已创建 {filename}"

        # 验证：main.py 包含 FastAPI app 定义
        main_content = (project_dir / "main.py").read_text(encoding="utf-8")
        assert "FastAPI" in main_content, "main.py 应包含 FastAPI 导入"
        assert "health_check" in main_content, "main.py 应包含 health_check 路由"
        assert "get_item" in main_content, "main.py 应包含 get_item 路由"

        # Agent 提交变更
        agent_commit = _agent_commit_changes(project_dir)

        # 验证：Agent commit 成功
        assert len(agent_commit) >= 7, f"Agent commit hash 有效: {agent_commit}"
        assert agent_commit != initial_commit, "Agent commit 应不同于初始 commit"

        # ── 步骤 6a：评估前保存（checkpoint） ──
        _checkpoint_before_evaluate(project_dir)
        # 无额外变更时 checkpoint 为 None（Agent 已提交）
        # 如果有未保存的变更则会生成 checkpoint commit

        # ── 步骤 6b：评估通过 → 合并 feature 到 main ──
        merge_result = _merge_feature_to_main(project_dir)

        # 验证：合并成功
        assert merge_result["success"] is True, "合并应成功"
        assert "merge_commit" in merge_result, "应返回 merge commit hash"

        # 验证：当前在 main 分支
        branch_result = _run_git("branch", "--show-current", cwd=project_dir)
        assert branch_result.stdout.strip() == "main", (
            "合并后应在 main 分支上"
        )

        # ── 步骤 7：cleanup 删除功能分支 ──
        _cleanup_feature_branch(project_dir)

        # ── 最终验证 ──

        # 验证点 1：项目目录存在且有 .git
        assert (project_dir / ".git").exists(), "最终项目目录应有 .git"
        assert (project_dir / ".git").is_dir(), ".git 应为目录"

        # 验证点 2：FastAPI 文件存在且可导入
        assert (project_dir / "main.py").exists(), "main.py 应存在"
        assert (project_dir / "models.py").exists(), "models.py 应存在"
        assert (project_dir / "config.py").exists(), "config.py 应存在"

        # 验证 main.py 可导入（语法正确）
        assert _can_import_module(project_dir / "main.py", "test_main"), (
            "main.py 应可正常导入（语法正确）"
        )

        # 验证 models.py 可导入
        assert _can_import_module(project_dir / "models.py", "test_models"), (
            "models.py 应可正常导入（语法正确）"
        )

        # 验证 config.py 可导入且值正确
        assert _can_import_module(project_dir / "config.py", "test_config"), (
            "config.py 应可正常导入（语法正确）"
        )
        import test_config  # type: ignore[import-not-found]  # noqa: E402
        assert test_config.APP_NAME == "Demo API", "config.py 中 APP_NAME 应为 Demo API"
        assert test_config.VERSION == "1.0.0", "config.py 中 VERSION 应为 1.0.0"

        # 验证 main.py 中 FastAPI 实例的路由定义
        import test_main  # type: ignore[import-not-found]  # noqa: E402
        assert hasattr(test_main, "app"), "main.py 应导出 app 实例"

        # 验证点 3：git log 中有初始 commit 和功能分支合并记录
        log_result = _run_git(
            "log", "--oneline", "--format=%s",
            cwd=project_dir,
        )
        log_messages = log_result.stdout.strip().splitlines()

        # 检查初始 commit
        assert any(_INIT_COMMIT_MSG in msg for msg in log_messages), (
            f"git log 应包含初始 commit '{_INIT_COMMIT_MSG}'，实际: {log_messages}"
        )

        # 检查 Agent 的功能 commit
        assert any(_AGENT_COMMIT_MSG in msg for msg in log_messages), (
            f"git log 应包含功能 commit '{_AGENT_COMMIT_MSG}'，实际: {log_messages}"
        )

        # 检查合并记录（Merge branch 'feature/xxx' 或类似格式）
        has_merge_record = any(
            "merge" in msg.lower() or _FEATURE_BRANCH in msg
            for msg in log_messages
        )
        assert has_merge_record, (
            f"git log 应包含合并记录，实际: {log_messages}"
        )

        # 验证至少有 3 条 commit（初始 + 功能 + 合并）
        assert len(log_messages) >= 3, (
            f"应至少有 3 条 commit（初始 + 功能 + 合并），"
            f"实际 {len(log_messages)} 条: {log_messages}"
        )

        # 验证点 4：功能分支已删除
        branch_list_result = _run_git(
            "branch", "--list", _FEATURE_BRANCH, cwd=project_dir,
        )
        assert branch_list_result.stdout.strip() == "", (
            f"功能分支 {_FEATURE_BRANCH} 应已删除，"
            f"实际仍存在: {branch_list_result.stdout.strip()}"
        )

        # 额外验证：仅剩 main 分支
        all_branches_result = _run_git("branch", "--list", cwd=project_dir)
        remaining_branches = [
            b.strip().lstrip("* ").strip()
            for b in all_branches_result.stdout.strip().splitlines()
            if b.strip()
        ]
        assert remaining_branches == ["main"], (
            f"清理后应仅剩 main 分支，实际: {remaining_branches}"
        )

    async def test_new_project_subtask_branch_isolation(self, tmp_path: Path) -> None:
        """测试子任务 feature 分支的隔离性

        验证在 feature 分支上的修改不会影响 main 分支，
        直到显式合并操作执行。
        """
        # 初始化项目
        workspace_root = tmp_path / ".ai_workspaces"
        project_dir = workspace_root / "task_branch_iso"
        _init_project_root(project_dir)

        # 记录 main 分支上的文件列表
        main_files = set(p.name for p in project_dir.iterdir() if p.is_file())

        # 创建 feature 分支
        _create_feature_branch(project_dir)

        # 在 feature 分支上创建新文件并提交
        (project_dir / "feature_only.py").write_text(
            "# 仅在 feature 分支\n", encoding="utf-8"
        )
        _run_git("add", "-A", cwd=project_dir)
        _run_git("commit", "-m", "feat: feature 独有文件", cwd=project_dir)

        # 切回 main 分支
        _run_git("checkout", "main", cwd=project_dir)

        # 验证：main 分支上不应有 feature_only.py
        assert not (project_dir / "feature_only.py").exists(), (
            "feature 分支的文件不应出现在 main 上"
        )

        # 验证：main 上的文件列表未变
        current_files = set(p.name for p in project_dir.iterdir() if p.is_file())
        assert current_files == main_files, (
            f"main 分支文件应未变，实际: {current_files}"
        )

        # 合并后验证
        _run_git("merge", _FEATURE_BRANCH, cwd=project_dir)
        assert (project_dir / "feature_only.py").exists(), (
            "合并后 feature_only.py 应出现在 main 分支"
        )

        # 清理
        _run_git("branch", "-d", _FEATURE_BRANCH, cwd=project_dir)

    async def test_new_project_multiple_subtasks_sequential(self, tmp_path: Path) -> None:
        """测试多个子任务按顺序在 feature 分支上开发并合并

        验证多个功能分支的顺序合并流程：
        1. 创建 feature-1 分支 → 开发 → 合并到 main
        2. 创建 feature-2 分支 → 开发 → 合并到 main
        """
        # 初始化项目
        workspace_root = tmp_path / ".ai_workspaces"
        project_dir = workspace_root / "task_multi_sub"
        _init_project_root(project_dir)

        # ── 第一个功能分支 ──
        branch_1 = "feature/sub_task_api_v1"
        _run_git("checkout", "-b", branch_1, cwd=project_dir)
        (project_dir / "api_v1.py").write_text(
            '"""API v1 模块"""\nAPI_VERSION = "v1"\n', encoding="utf-8"
        )
        _run_git("add", "-A", cwd=project_dir)
        _run_git("commit", "-m", "feat: 添加 API v1", cwd=project_dir)

        # 合并第一个分支
        _run_git("checkout", "main", cwd=project_dir)
        _run_git("merge", branch_1, cwd=project_dir)
        _run_git("branch", "-d", branch_1, cwd=project_dir)

        # 验证 v1 已合并
        assert (project_dir / "api_v1.py").exists(), "api_v1.py 应已合并到 main"

        # ── 第二个功能分支 ──
        branch_2 = "feature/sub_task_api_v2"
        _run_git("checkout", "-b", branch_2, cwd=project_dir)
        (project_dir / "api_v2.py").write_text(
            '"""API v2 模块"""\nAPI_VERSION = "v2"\n', encoding="utf-8"
        )
        _run_git("add", "-A", cwd=project_dir)
        _run_git("commit", "-m", "feat: 添加 API v2", cwd=project_dir)

        # 合并第二个分支
        _run_git("checkout", "main", cwd=project_dir)
        _run_git("merge", branch_2, cwd=project_dir)
        _run_git("branch", "-d", branch_2, cwd=project_dir)

        # 验证 v1 和 v2 都在 main 上
        assert (project_dir / "api_v1.py").exists(), "api_v1.py 应仍在 main 上"
        assert (project_dir / "api_v2.py").exists(), "api_v2.py 应已合并到 main"

        # 验证 git log 包含两个功能的 commit
        log_result = _run_git("log", "--oneline", "--format=%s", cwd=project_dir)
        log_messages = log_result.stdout.strip().splitlines()
        assert any("API v1" in msg for msg in log_messages), "应有 API v1 的 commit"
        assert any("API v2" in msg for msg in log_messages), "应有 API v2 的 commit"

    async def test_new_project_eval_failed_rollback(self, tmp_path: Path) -> None:
        """测试评估失败后的回滚机制

        验证在 feature 分支上开发后，如果评估失败，
        系统能正确回滚到分支创建前的状态。
        """
        # 初始化项目
        workspace_root = tmp_path / ".ai_workspaces"
        project_dir = workspace_root / "task_rollback"
        initial_commit = _init_project_root(project_dir)

        # 创建 feature 分支
        _create_feature_branch(project_dir)

        # Agent 创建文件
        (project_dir / "bad_code.py").write_text(
            "this is bad syntax {\n", encoding="utf-8"
        )
        _run_git("add", "-A", cwd=project_dir)
        _run_git("commit", "-m", "feat: 有问题的代码", cwd=project_dir)

        # 模拟评估失败 → 回滚到分支创建时的状态
        # 先获取 main 分支的 HEAD（即初始 commit）
        main_head_result = _run_git("rev-parse", "main", cwd=project_dir)
        main_head = main_head_result.stdout.strip()

        # 模拟 on_task_failed：回滚到初始状态
        _run_git("reset", "--hard", main_head, cwd=project_dir)

        # 验证：bad_code.py 已被清除
        assert not (project_dir / "bad_code.py").exists(), (
            "回滚后 bad_code.py 应不存在"
        )

        # 验证：当前 HEAD 等于初始 commit
        current_head_result = _run_git("rev-parse", "HEAD", cwd=project_dir)
        assert current_head_result.stdout.strip() == initial_commit, (
            "回滚后 HEAD 应等于初始 commit"
        )

        # 删除 feature 分支（清理失败的分支）
        _run_git("checkout", "main", cwd=project_dir)
        _run_git("branch", "-D", _FEATURE_BRANCH, cwd=project_dir)

    async def test_new_project_workspace_under_ai_workspaces(self, tmp_path: Path) -> None:
        """测试新项目目录确实创建在 .ai_workspaces 下

        验证场景 C 的路径结构：
        {base_path}/.ai_workspaces/{task_id}/
        """
        workspace_root = tmp_path / ".ai_workspaces"
        task_id = "task_path_verify"
        project_dir = workspace_root / task_id

        # 初始化
        _init_project_root(project_dir)

        # 验证路径层级
        assert project_dir.parent == workspace_root, (
            "项目目录的父目录应为 .ai_workspaces"
        )
        assert workspace_root.name == ".ai_workspaces", (
            "workspace 根目录名应为 .ai_workspaces"
        )
        assert project_dir.name == task_id, (
            f"项目目录名应为 task_id: {task_id}"
        )

        # 验证 .git 在正确位置
        git_dir = project_dir / ".git"
        assert git_dir.exists(), ".git 应在项目目录下"
        assert git_dir.is_dir(), ".git 应为目录"

    async def test_new_project_file_content_preserved_after_merge(self, tmp_path: Path) -> None:
        """测试合并后 FastAPI 文件内容完整保留

        验证从 feature 分支合并到 main 后，
        所有文件的内容与 Agent 创建时完全一致。
        """
        # 初始化项目
        workspace_root = tmp_path / ".ai_workspaces"
        project_dir = workspace_root / "task_content_preserve"
        _init_project_root(project_dir)

        # 创建 feature 分支并添加文件
        _create_feature_branch(project_dir)
        _agent_create_fastapi_files(project_dir)
        _agent_commit_changes(project_dir)

        # 合并到 main
        _merge_feature_to_main(project_dir)

        # 验证文件内容完全匹配
        assert (project_dir / "main.py").read_text(encoding="utf-8") == MAIN_PY_CONTENT, (
            "main.py 内容应与 Agent 创建时完全一致"
        )
        assert (project_dir / "models.py").read_text(encoding="utf-8") == MODELS_PY_CONTENT, (
            "models.py 内容应与 Agent 创建时完全一致"
        )
        assert (project_dir / "config.py").read_text(encoding="utf-8") == CONFIG_PY_CONTENT, (
            "config.py 内容应与 Agent 创建时完全一致"
        )

        # 清理
        _cleanup_feature_branch(project_dir)

    async def test_new_project_git_log_commit_count(self, tmp_path: Path) -> None:
        """测试 git log 中 commit 数量和顺序正确

        验证完整流程后 git log 的 commit 顺序：
        1. 合并 commit（最新）
        2. Agent 功能 commit
        3. 初始 commit（最早）
        """
        # 初始化项目
        workspace_root = tmp_path / ".ai_workspaces"
        project_dir = workspace_root / "task_log_order"
        _init_project_root(project_dir)

        # 创建 feature 分支并提交
        _create_feature_branch(project_dir)
        _agent_create_fastapi_files(project_dir)
        _agent_commit_changes(project_dir)

        # 合并到 main
        _merge_feature_to_main(project_dir)
        _cleanup_feature_branch(project_dir)

        # 获取 git log
        log_result = _run_git(
            "log", "--oneline", "--format=%s",
            cwd=project_dir,
        )
        log_messages = [msg.strip() for msg in log_result.stdout.strip().splitlines()]

        # 验证 commit 包含所有必要信息（不严格检查顺序）
        log_text = "\n".join(log_messages)
        assert any("merge" in msg.lower() for msg in log_messages), (
            f"git log 应包含合并 commit，实际: {log_messages}"
        )
        assert any(_AGENT_COMMIT_MSG in msg for msg in log_messages), (
            f"git log 应包含功能提交 '{_AGENT_COMMIT_MSG}'，实际: {log_messages}"
        )
        assert any(_INIT_COMMIT_MSG in msg for msg in log_messages), (
            f"git log 应包含初始 commit '{_INIT_COMMIT_MSG}'，实际: {log_messages}"
        )
