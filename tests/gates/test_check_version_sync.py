# @feature: FP-GATE 三端版本同步闸（AR-M10） | @ci: python-coverage
"""check_version_sync.py 单测（AR-M10 三端统一版本号）。

行为契约（断输入→输出，不钉实现）：
- 六面同值 → check_sync 返回空错误列表（绿）；多组有区分度真值（0.2.0 / 1.0.0）
- 任一面漂移 → 红，报错点名该面并含双侧值（单侧漏改矩阵：六面逐一）；
- fail-closed：VERSION 缺失 / 格式非三段式 / 任一面文件缺失或不可解析 /
  versionName 多处声明 → 红——闸不允许退化成永绿假闸；
- [workspace.package] 段锚定：他段 version 字样（如 [workspace.dependencies]）
  不入抽取面；
- 删值实验（验收①）：复制真仓六面文件、仅改 VERSION → 必须红且报出三端现值；
  不改 → 绿（真形态对照）；
- 负控制（防恒绿）：真实仓状态六面一致，检查器子进程实跑退出 0。
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
REPO_ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.unit


def _load():
    spec = importlib.util.spec_from_file_location(
        "check_version_sync", SCRIPTS / "check_version_sync.py"
    )
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


mod = _load()

CARGO_TEMPLATE = """[workspace.package]
version = "{version}"
edition = "2021"

[workspace.dependencies]
tokio = {{ version = "1", features = ["full"] }}
"""
GRADLE_TEMPLATE = """android {{
    defaultConfig {{
        versionCode 23
        versionName "{version}"
    }}
}}
"""


def make_repo(root: Path, version: str) -> Path:
    """最小六面仓：VERSION + 三端五个版本引用面，全部同值。"""
    (root / "VERSION").write_text(f"{version}\n", encoding="utf-8")
    (root / "package.json").write_text(
        json.dumps({"name": "agentos", "version": version}), encoding="utf-8"
    )
    (root / "kernel").mkdir()
    (root / "kernel" / "Cargo.toml").write_text(
        CARGO_TEMPLATE.format(version=version), encoding="utf-8"
    )
    for rel in ("frontend", "app-mobile"):
        (root / rel).mkdir()
        (root / rel / "package.json").write_text(
            json.dumps({"name": rel, "version": version}), encoding="utf-8"
        )
    gradle_dir = root / "app-mobile" / "android" / "app"
    gradle_dir.mkdir(parents=True)
    (gradle_dir / "build.gradle").write_text(
        GRADLE_TEMPLATE.format(version=version), encoding="utf-8"
    )
    return root


def surface_error(errors: list[str], needle: str) -> str:
    hits = [e for e in errors if needle in e]
    assert len(hits) == 1, f"期望恰一条报错点名 {needle!r}，实际：{errors}"
    return hits[0]


@pytest.mark.parametrize("version", ["0.2.0", "1.0.0"])  # 现值 + 换档值
def test_all_surfaces_match_green(version: str, tmp_path: Path) -> None:
    make_repo(tmp_path, version)
    root_version, errors = mod.check_sync(tmp_path)
    assert errors == []
    assert root_version == version


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r, v: (r / "kernel" / "Cargo.toml").write_text(
            CARGO_TEMPLATE.format(version=v), encoding="utf-8"
        ),
        lambda r, v: (r / "package.json").write_text(
            json.dumps({"version": v}), encoding="utf-8"
        ),
        lambda r, v: (r / "frontend" / "package.json").write_text(
            json.dumps({"version": v}), encoding="utf-8"
        ),
        lambda r, v: (r / "app-mobile" / "package.json").write_text(
            json.dumps({"version": v}), encoding="utf-8"
        ),
        lambda r, v: (r / "app-mobile" / "android" / "app" / "build.gradle").write_text(
            GRADLE_TEMPLATE.format(version=v), encoding="utf-8"
        ),
    ],
    ids=["cargo", "root-json", "frontend-json", "mobile-json", "gradle"],
)
def test_single_surface_drift_red(mutate, tmp_path: Path) -> None:
    make_repo(tmp_path, "0.2.0")
    mutate(tmp_path, "9.9.9")  # 单侧漏改：其余五面仍 0.2.0
    root_version, errors = mod.check_sync(tmp_path)
    assert root_version == "0.2.0"
    assert len(errors) == 1
    assert "9.9.9" in errors[0]
    assert "0.2.0" in errors[0]


def test_missing_version_file_red(tmp_path: Path) -> None:
    make_repo(tmp_path, "0.2.0")
    (tmp_path / "VERSION").unlink()
    root_version, errors = mod.check_sync(tmp_path)
    assert root_version == ""
    assert len(errors) == 1
    assert "缺失" in errors[0]


@pytest.mark.parametrize("bad", ["0.2", "v0.2.0", "0.2.0.1", "abc", ""])
def test_malformed_version_red(bad: str, tmp_path: Path) -> None:
    make_repo(tmp_path, "0.2.0")
    (tmp_path / "VERSION").write_text(f"{bad}\n", encoding="utf-8")
    _, errors = mod.check_sync(tmp_path)
    assert len(errors) == 1
    assert "三段式" in errors[0]


@pytest.mark.parametrize(
    ("mutate", "needle"),
    [
        (
            lambda r: (r / "kernel" / "Cargo.toml").write_text(
                '[workspace.dependencies]\ntokio = { version = "1" }\n', encoding="utf-8"
            ),
            "kernel/Cargo.toml",
        ),
        (
            lambda r: (r / "package.json").write_text('{"name": "agentos"}', encoding="utf-8"),
            "package.json",
        ),
        (
            lambda r: (r / "frontend" / "package.json").write_text(
                '{"name": "frontend"}', encoding="utf-8"
            ),
            "frontend/package.json",
        ),
        (
            lambda r: (r / "app-mobile" / "android" / "app" / "build.gradle").write_text(
                "android {\n}\n", encoding="utf-8"
            ),
            "build.gradle",
        ),
        (
            lambda r: (r / "app-mobile" / "package.json").unlink(),
            "app-mobile/package.json",
        ),
    ],
    ids=["cargo-no-section", "root-json-no-key", "json-no-key", "gradle-no-versionname", "file-missing"],
)
def test_unparseable_surface_fail_closed(mutate, needle: str, tmp_path: Path) -> None:
    make_repo(tmp_path, "0.2.0")
    mutate(tmp_path)
    _, errors = mod.check_sync(tmp_path)
    assert surface_error(errors, needle)


def test_duplicate_gradle_versionname_red(tmp_path: Path) -> None:
    make_repo(tmp_path, "0.2.0")
    gradle = tmp_path / "app-mobile" / "android" / "app" / "build.gradle"
    gradle.write_text(
        GRADLE_TEMPLATE.format(version="0.2.0") + 'versionName "0.3.0"\n', encoding="utf-8"
    )
    _, errors = mod.check_sync(tmp_path)
    assert surface_error(errors, "build.gradle")
    assert not any("不可解析" in e for e in errors)  # 歧义单独报，不重复报泛化错


def test_real_repo_delete_value_experiment(tmp_path: Path) -> None:
    """删值实验（验收①）：真仓六面复制过来，仅改 VERSION → 必须红。

    改动值若不进报错（假闸硬编码期望值）则本用例红不了——防实现刷绿。
    """
    for rel in (
        "VERSION",
        "package.json",
        "kernel/Cargo.toml",
        "frontend/package.json",
        "app-mobile/package.json",
        "app-mobile/android/app/build.gradle",
    ):
        src = REPO_ROOT / rel
        dst = tmp_path / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(src, dst)

    root_version, errors = mod.check_sync(tmp_path)
    assert root_version == "0.2.2"
    assert errors == []  # 真形态对照：不改则绿

    (tmp_path / "VERSION").write_text("9.9.9\n", encoding="utf-8")
    root_version, errors = mod.check_sync(tmp_path)
    assert root_version == "9.9.9"
    assert len(errors) == 5  # 其余五面全部点名漂移
    for e in errors:
        assert "0.2.2" in e
        assert "9.9.9" in e


def test_real_repo_subprocess_green() -> None:
    """负控制（防恒绿）：真仓现状六面一致，检查器实跑退出 0。"""
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "check_version_sync.py")],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, f"stderr={proc.stderr}"
