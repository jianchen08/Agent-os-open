# @feature: FP-RELEASE 统一交付清单生成器 | @ci: python-coverage
"""make_delivery_manifest.py 单测（全客户端统一发布管线）。

行为契约（断输入→输出，不钉实现）：
- 正常路径：多产物 → manifest 六字段齐、digest 与 bytes 与实物一致、path 相对
  manifest 目录（真值实验：对临时文件实算 sha256 对照）；
- 可复现性：同输入 released_at 显式钉值时输出逐字段确定（digest 稳定）；
- fail-closed：产物缺失 / 名称重复 / semver 非三段式 / version_code 负数 /
  build_head 缺失 → ValueError，不产出半份清单；
- path 相对化：产物在 manifest 目录之外时保留绝对路径（可搬移语义只在目录内成立）；
- CLI：--artifact 形态执法（名称=路径）。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parents[2] / "deploy"

pytestmark = pytest.mark.unit


def _load():
    spec = importlib.util.spec_from_file_location(
        "make_delivery_manifest", DEPLOY / "make_delivery_manifest.py"
    )
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


mod = _load()


def _write_artifact(root: Path, rel: str, payload: bytes) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def test_manifest_green_fields_and_digest(tmp_path: Path) -> None:
    out_dir = tmp_path / "release" / "0.2.1"
    out_dir.mkdir(parents=True)
    apk = _write_artifact(out_dir, "lingxi-agentos-arm64-phone.apk", b"apk-payload-\x00\x01")
    tar = _write_artifact(out_dir, "agentos-cloud-0.2.1.tar.gz", b"tar-payload")

    manifest = mod.build_manifest(
        out_dir,
        semver="0.2.1",
        version_code=24,
        build_head="abc123def456",
        artifacts=[("apk-arm64", apk), ("cloud-tar", tar)],
        released_at="2026-10-16T00:00:00+00:00",
    )

    assert manifest["schema_version"] == 1
    assert manifest["released_at"] == "2026-10-16T00:00:00+00:00"
    assert manifest["build_head"] == "abc123def456"
    assert manifest["version"] == {"semver": "0.2.1", "version_code": 24}
    assert set(manifest["artifacts"]) == {"apk-arm64", "cloud-tar"}

    entry = manifest["artifacts"]["apk-arm64"]
    assert entry["path"] == "lingxi-agentos-arm64-phone.apk"  # 相对 manifest 目录
    assert entry["sha256"] == hashlib.sha256(b"apk-payload-\x00\x01").hexdigest()  # 真值对照
    assert entry["bytes"] == len(b"apk-payload-\x00\x01")


def test_manifest_deterministic_under_pinned_time(tmp_path: Path) -> None:
    out_dir = tmp_path / "rel"
    out_dir.mkdir()
    payload = bytes(range(256)) * 4096  # >1MB，跨 chunk 边界（流式读取面）
    apk = _write_artifact(out_dir, "a.bin", payload)

    kwargs = {
        "out_dir": out_dir,
        "semver": "1.0.0",
        "version_code": 7,
        "build_head": "h" * 40,
        "artifacts": [("a", apk)],
        "released_at": "2026-01-01T00:00:00+00:00",
    }
    assert mod.build_manifest(**kwargs) == mod.build_manifest(**kwargs)  # 同输入同输出
    assert mod.build_manifest(**kwargs)["artifacts"]["a"]["sha256"] == hashlib.sha256(payload).hexdigest()


@pytest.mark.parametrize(
    ("semver", "code", "head", "needle"),
    [
        ("0.2", 24, "head", "三段式"),
        ("0.2.0.1", 24, "head", "三段式"),
        ("0.2.1", -1, "head", "version_code"),
        ("0.2.1", 24, "", "build_head"),
    ],
)
def test_manifest_fail_closed_scalar(semver: str, code: int, head: str, needle: str, tmp_path: Path) -> None:
    out_dir = tmp_path / "rel"
    out_dir.mkdir()
    with pytest.raises(ValueError, match=needle):
        mod.build_manifest(out_dir, semver=semver, version_code=code, build_head=head, artifacts=[])


def test_manifest_fail_closed_missing_artifact(tmp_path: Path) -> None:
    out_dir = tmp_path / "rel"
    out_dir.mkdir()
    with pytest.raises(ValueError, match="产物文件缺失"):
        mod.build_manifest(out_dir, "0.2.1", 24, "head", [("a", out_dir / "missing.bin")])


def test_manifest_fail_closed_duplicate_name(tmp_path: Path) -> None:
    out_dir = tmp_path / "rel"
    out_dir.mkdir()
    artifact = _write_artifact(out_dir, "a.bin", b"x")
    with pytest.raises(ValueError, match="产物名称重复"):
        mod.build_manifest(out_dir, "0.2.1", 24, "head", [("n", artifact), ("n", artifact)])


def test_manifest_outside_artifact_keeps_absolute_path(tmp_path: Path) -> None:
    out_dir = tmp_path / "rel"
    out_dir.mkdir()
    outside = _write_artifact(tmp_path, "elsewhere/a.bin", b"y")
    manifest = mod.build_manifest(out_dir, "0.2.1", 24, "head", [("a", outside)])
    assert Path(manifest["artifacts"]["a"]["path"]).is_absolute()


def test_cli_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out_dir = tmp_path / "release" / "0.2.1"
    out_dir.mkdir(parents=True)
    _write_artifact(out_dir, "a.bin", b"cli")

    manifest_path = out_dir / "delivery_manifest.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "make_delivery_manifest.py",
            "--out", str(manifest_path),
            "--semver", "0.2.1",
            "--version-code", "24",
            "--build-head", "deadbeef",
            "--artifact", f"bin={out_dir / 'a.bin'}",
        ],
    )
    assert mod.main() == 0
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["artifacts"]["bin"]["sha256"] == hashlib.sha256(b"cli").hexdigest()


def test_cli_bad_artifact_form_exit_2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "make_delivery_manifest.py",
            "--out", str(tmp_path / "m.json"),
            "--semver", "0.2.1",
            "--version-code", "24",
            "--build-head", "h",
            "--artifact", "no-separator-form",
        ],
    )
    assert mod.main() == 2
    assert "名称=路径" in capsys.readouterr().err
