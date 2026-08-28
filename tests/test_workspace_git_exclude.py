# @feature: FP-0.2.〇 任务执行驱动 | @ci: python-coverage
"""ensure_workspace_git_ignored：工作空间基目录的 git 本地排除契约。

基目录是配置项（workspace.root）——静态 .gitignore 只在特定配置值下成立。
exclude 随解析出的实际根动态落地（.git/info/exclude，不入库、git 还原
不触碰），本文件锁定：仓库内基目录写排除且幂等、仓库外放行、基目录即
项目根拒绝、无 .git 放行。
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture()
def proj(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """临时项目：.git/ + config/isolation/isolation_config.yaml 标记
    （find_project_root 的 env 校验要求该 yaml 存在），AGENTOS_CONFIG_ROOT 指向之。"""
    (tmp_path / ".git" / "info").mkdir(parents=True)
    (tmp_path / "config" / "isolation").mkdir(parents=True)
    (tmp_path / "config" / "isolation" / "isolation_config.yaml").write_text("", encoding="utf-8")
    monkeypatch.setenv("AGENTOS_CONFIG_ROOT", str(tmp_path / "config"))
    return tmp_path


def _import():
    import tests._isolation_path  # noqa: F401
    from isolation.workspace import ensure_workspace_git_ignored

    return ensure_workspace_git_ignored


def test_base_inside_repo_gets_exclude_entry(proj: Path) -> None:
    fn = _import()
    base = proj / "wsroot"

    assert fn(base) is True
    exclude = proj / ".git" / "info" / "exclude"
    text = exclude.read_text(encoding="utf-8")
    assert "/wsroot/" in text

    # 幂等：重跑不重复写条目
    fn(base)
    lines = [ln for ln in text.splitlines() if ln.strip() == "/wsroot/"]
    assert len(lines) == 1
    assert len(exclude.read_text(encoding="utf-8").splitlines()) == len(text.splitlines())


def test_base_outside_repo_passes_without_exclude(
    proj: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    # 独立的第二个根（tmp_path 与 proj 同源，放它下面等于"仓库内"）
    other_root = tmp_path_factory.mktemp("outside")
    fn = _import()
    outside = other_root / "ws"
    outside.mkdir(parents=True)

    assert fn(outside) is True
    assert not (proj / ".git" / "info" / "exclude").exists()


def test_base_equal_project_root_rejected(proj: Path) -> None:
    fn = _import()

    assert fn(proj) is False
    exclude = proj / ".git" / "info" / "exclude"
    if exclude.exists():
        assert "/." not in exclude.read_text(encoding="utf-8")


def test_no_git_repo_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 项目标记在但无 .git → 无追踪面，放行且不创建任何文件
    (tmp_path / "config" / "isolation").mkdir(parents=True)
    (tmp_path / "config" / "isolation" / "isolation_config.yaml").write_text("", encoding="utf-8")
    monkeypatch.setenv("AGENTOS_CONFIG_ROOT", str(tmp_path / "config"))
    fn = _import()

    assert fn(tmp_path / "ws") is True
    assert not (tmp_path / ".git").exists()
