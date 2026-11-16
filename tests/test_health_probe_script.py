# @feature: FP-0.2.一 五层健康探针 | @ci: python-coverage
"""health_probe 脚本契约测试（日志面根发现 + 登录口令来源）。

锁两条 2026-11-05 装机轮修好的行为：
- 自定义安装位（NSIS `/D=` 装到任意盘）的日志根按**活体内核 exe 同目录**
  派生，否则日志面对 D: 盘装机误红；
- 登录口令环境变量优先、缺省回读用户空间 `.env`（口令恒读 .env 纪律），
  两处都无才判 no-password（非红项）。

加载经 importlib 唯名模块（scripts/ 非包，防裸名串扰）；`_REPO` 一律指
tmp_path，测试不读真实仓 logs/。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "health_probe.py"


@pytest.fixture
def mod(tmp_path: Path) -> Any:
    spec = importlib.util.spec_from_file_location("health_probe_test_mod", _SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    m = importlib.util.module_from_spec(spec)
    sys.modules["health_probe_test_mod"] = m
    spec.loader.exec_module(m)
    m._REPO = tmp_path  # type: ignore[attr-defined]
    return m


def _kernel_face(exe: str) -> dict[str, Any]:
    return {"items": [{"kind": "kernel", "pid": 123, "exe": exe}], "kernel_count": 1}


def test_log_roots_include_live_kernel_sibling_logs(mod: Any, tmp_path: Path) -> None:
    """自定义安装位：日志根从活体内核 exe 的上级目录派生（与盘符无关）。"""
    install_logs = tmp_path / "custom_install" / "resources" / "kernel" / "logs"
    install_logs.mkdir(parents=True)
    exe = install_logs.parent / "agentos-kernel.exe"

    roots = mod._log_roots(_kernel_face(str(exe)))

    assert install_logs in roots


def test_log_roots_ignore_missing_or_unknown_exe(mod: Any) -> None:
    """exe 缺失/不可判（'?'）不产生凭空根，且默认候选保持去重。"""
    roots = mod._log_roots({"items": [{"kind": "kernel", "pid": 1, "exe": "?"}, {"kind": "electron"}]})

    assert roots.count(mod._REPO / "logs") == 1
    assert len(roots) == len(set(roots))


def test_probe_logs_fresh_install_root_clears_stale_repo_root(mod: Any, tmp_path: Path) -> None:
    """装机日志根新鲜时，陈旧的仓根降为记录项而不是红项（多根裁定）。"""
    install_logs = tmp_path / "custom_install" / "resources" / "kernel" / "logs"
    install_logs.mkdir(parents=True)
    (install_logs / "kernel.liveness").write_text("", encoding="utf-8")
    exe = install_logs.parent / "agentos-kernel.exe"
    repo_logs = mod._REPO / "logs"
    repo_logs.mkdir(parents=True, exist_ok=True)  # 存在但无 liveness → 陈旧

    face = mod._probe_logs(mod._utcnow(), _kernel_face(str(exe)))

    assert face["red"] == []
    fresh = [i for i in face["items"] if i["root"] == str(install_logs)]
    assert fresh, "装机日志根必须在候选里"
    assert fresh[0]["fresh"] is True


def test_probe_logs_all_stale_is_red(mod: Any, tmp_path: Path) -> None:
    """没有任何新鲜判活面 → 红（不能因根存在就放行）。"""
    repo_logs = mod._REPO / "logs"
    repo_logs.mkdir(parents=True, exist_ok=True)

    face = mod._probe_logs(mod._utcnow(), {"items": []})

    assert face["red"], "全部根陈旧必须报红"


def test_admin_password_prefers_user_space_env_file(
    mod: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`.env` 是真值面：即使 shell 环境变量有值（历史快照）也用 .env。"""
    monkeypatch.setenv("AGENTOS_ADMIN_PASSWORD", "stale-env-pw")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    env_dir = tmp_path / "agentos"
    env_dir.mkdir()
    (env_dir / ".env").write_text("AGENTOS_ADMIN_PASSWORD=file-pw\r\n", encoding="utf-8")

    assert mod._admin_password() == "file-pw"


def test_admin_password_falls_back_to_env_when_file_absent(
    mod: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """.env 缺失（APPDATA 下无文件）→ 环境变量兜底。"""
    monkeypatch.setenv("AGENTOS_ADMIN_PASSWORD", "env-pw")
    monkeypatch.setenv("APPDATA", str(tmp_path))

    assert mod._admin_password() == "env-pw"


def test_admin_password_falls_back_to_user_space_env_file(
    mod: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """环境变量缺失 → 回读用户空间 .env（容忍 CRLF 与空值）。"""
    monkeypatch.delenv("AGENTOS_ADMIN_PASSWORD", raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path))
    env_dir = tmp_path / "agentos"
    env_dir.mkdir()
    (env_dir / ".env").write_text("OTHER=1\r\nAGENTOS_ADMIN_PASSWORD=file-pw\r\n", encoding="utf-8")

    assert mod._admin_password() == "file-pw"


def test_admin_password_none_when_absent(mod: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """两处都无（或 .env 值为空）→ None，登录探活降级为非红记录。"""
    monkeypatch.delenv("AGENTOS_ADMIN_PASSWORD", raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path))
    env_dir = tmp_path / "agentos"
    env_dir.mkdir()
    (env_dir / ".env").write_text("AGENTOS_ADMIN_PASSWORD=\r\n", encoding="utf-8")

    assert mod._admin_password() is None


def test_probe_runs_reports_schema_no_runs(mod: Any, tmp_path: Path) -> None:
    """0.2 扁平 schema（无 runs 表）→ 标注不适用，而非渲染成读失败。"""
    import sqlite3

    db = tmp_path / "agentos_kernel.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE pipeline_state (pipeline_id TEXT, field_key TEXT, value TEXT)")
    conn.commit()
    conn.close()

    face = mod._probe_runs(db, mod._utcnow())

    assert face["status"] == "schema-no-runs"
    assert "error" not in face
