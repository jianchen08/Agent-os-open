# @feature: FP-DEPLOY deploy-manifest 回读校验（diff 更新是否落地） | @ci: python-coverage
"""verify_deploy_manifest.py 单测（打包清单生成 / 部署位回读比对）。

行为契约（断输入→输出，不钉实现）：
- build：清单逐文件 sha256 与实物一致（真值实算对照）、键排序确定、关键项自动
  标记（plugin.json / 内核 exe / frontend index / config/kernel yaml / host.py /
  app.asar）、排除面生效（.venv* / __pycache__ / *.pyc / node_modules / target）；
- build fail-closed：profile 未知 / version 空 / 部署面根缺失 / 无文件 → ManifestError；
- verify：missing / mismatch / extra 三分类、critical 标记、运行期自写入面
  （kernel/logs、plugins/**/data）不误判为旧版本残留；
- 退出码：非严格档只告警 → 0，关键项 finding → 1，严格档任何 finding → 1，
  环境不可判 → 2；
- baseline：输出本次变更集与未落地数；
- 清单读面 fail-closed：schema / 字段类型 / critical 越界。
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
        "verify_deploy_manifest", DEPLOY / "verify_deploy_manifest.py"
    )
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


mod = _load()


def _write(root: Path, rel: str, payload: bytes) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def _fake_windows_tree(root: Path) -> None:
    """最小 windows profile 部署树（七个 managed root 齐备）。"""
    _write(root, "kernel/agentos-kernel.exe", b"kernel-binary")
    _write(root, "app.asar", b"asar-payload")
    _write(root, "frontend/dist/index.html", b"<html></html>")
    _write(root, "frontend/dist/assets/app.js", b"console.log(1)")
    _write(root, "plugins/shared/alpha/plugin.json", b'{"id":"alpha"}')
    _write(root, "plugins/shared/alpha/main.py", b"print('alpha')")
    _write(root, "plugins/shared/_host/host.py", b"print('host')")
    _write(root, "config/kernel/instance.yaml", b"schema: 1\n")
    _write(root, "skills/demo/SKILL.md", b"# demo\n")
    _write(root, "prereqs/python/python.exe", b"python-binary")
    _write(root, "bin/rclone.exe", b"rclone-binary")


def _build(root: Path, **kwargs):
    params = {"profile": "windows", "version": "0.2.2", "build_head": "abc123"}
    params.update(kwargs)
    return mod.build_manifest(root, **params)


def test_build_manifest_hashes_and_critical_marking(tmp_path: Path) -> None:
    _fake_windows_tree(tmp_path)
    manifest = _build(tmp_path, generated_at="2026-11-10T00:00:00+00:00")

    assert manifest["schema"] == 1
    assert manifest["profile"] == "windows"
    assert manifest["version"] == "0.2.2"
    assert manifest["build_head"] == "abc123"
    assert manifest["generated_at"] == "2026-11-10T00:00:00+00:00"
    assert list(manifest["files"]) == sorted(manifest["files"])  # 键序确定

    entry = manifest["files"]["kernel/agentos-kernel.exe"]
    assert entry["sha256"] == hashlib.sha256(b"kernel-binary").hexdigest()  # 真值对照
    assert entry["bytes"] == len(b"kernel-binary")

    critical = set(manifest["critical"])
    assert {
        "kernel/agentos-kernel.exe",
        "app.asar",
        "frontend/dist/index.html",
        "plugins/shared/alpha/plugin.json",
        "plugins/shared/_host/host.py",
        "config/kernel/instance.yaml",
    } <= critical
    assert "plugins/shared/alpha/main.py" not in critical
    assert "skills/demo/SKILL.md" not in critical


def test_build_manifest_excludes_env_and_cache_noise(tmp_path: Path) -> None:
    _fake_windows_tree(tmp_path)
    _write(tmp_path, "plugins/shared/alpha/.venv/Scripts/python.exe", b"venv")
    _write(tmp_path, "plugins/shared/alpha/.venv-win/Lib/site.py", b"venv2")
    _write(tmp_path, "plugins/shared/alpha/__pycache__/main.cpython-312.pyc", b"pyc")
    _write(tmp_path, "plugins/shared/alpha/stale.pyc", b"pyc2")
    _write(tmp_path, "plugins/shared/alpha/node_modules/x/index.js", b"js")
    _write(tmp_path, "kernel/target/debug/agentos-kernel.exe", b"target")

    manifest = _build(tmp_path)
    files = manifest["files"]
    assert not any(".venv" in rel for rel in files)
    assert not any("__pycache__" in rel or rel.endswith(".pyc") for rel in files)
    assert not any("node_modules" in rel for rel in files)
    assert not any(rel.startswith("kernel/target/") for rel in files)


def test_build_manifest_skips_out_path_inside_root(tmp_path: Path) -> None:
    _fake_windows_tree(tmp_path)
    out = tmp_path / "deploy-manifest.json"
    out.write_text("{}", encoding="utf-8")  # 已存在的旧清单（--rebaseline 场景）
    manifest = _build(tmp_path, skip_paths=(out,))
    assert "deploy-manifest.json" not in manifest["files"]


def test_build_manifest_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(mod.ManifestError, match="profile"):
        mod.build_manifest(tmp_path, profile="bogus", version="0.2.2")
    with pytest.raises(mod.ManifestError, match="version"):
        mod.build_manifest(tmp_path, profile="windows", version="")
    with pytest.raises(mod.ManifestError, match="根目录缺失"):
        mod.build_manifest(tmp_path, profile="windows", version="0.2.2")
    bare = tmp_path / "bare"
    for name in mod.PROFILE_ROOTS["windows"]:
        (bare / name).mkdir(parents=True)
    with pytest.raises(mod.ManifestError, match="顶层必检文件缺失"):
        mod.build_manifest(bare, profile="windows", version="0.2.2")
    empty = tmp_path / "empty"
    for name in mod.PROFILE_ROOTS["linux"]:
        (empty / name).mkdir(parents=True)
    with pytest.raises(mod.ManifestError, match="无可校验文件"):
        mod.build_manifest(empty, profile="linux", version="0.2.2")


def test_verify_classifies_missing_mismatch_extra(tmp_path: Path) -> None:
    _fake_windows_tree(tmp_path)
    manifest = _build(tmp_path)

    (tmp_path / "skills" / "demo" / "SKILL.md").write_bytes(b"# changed\n")  # 非关键 mismatch
    (tmp_path / "kernel" / "agentos-kernel.exe").unlink()  # 关键 missing
    _write(tmp_path, "plugins/shared/alpha/leftover.py", b"old")  # extra
    _write(tmp_path, "kernel/logs/kernel.log.2026-11-10", b"runtime")  # 运行期产物
    _write(tmp_path, "plugins/shared/alpha/data/cache.db", b"runtime")  # 用户数据目录

    findings = mod.verify_deployment(tmp_path, manifest, check_extra=True)
    by_kind = {(f.kind, f.path) for f in findings}
    assert ("missing", "kernel/agentos-kernel.exe") in by_kind
    assert ("mismatch", "skills/demo/SKILL.md") in by_kind
    assert ("extra", "plugins/shared/alpha/leftover.py") in by_kind
    assert not any(f.path.startswith("kernel/logs/") for f in findings)
    assert not any("/data/" in f.path for f in findings)
    critical_paths = {f.path for f in findings if f.critical}
    assert critical_paths == {"kernel/agentos-kernel.exe"}


def test_verify_without_extra_ignores_leftovers(tmp_path: Path) -> None:
    _fake_windows_tree(tmp_path)
    manifest = _build(tmp_path)
    _write(tmp_path, "skills/legacy/SKILL.md", b"old")
    assert mod.verify_deployment(tmp_path, manifest, check_extra=False) == []


@pytest.mark.parametrize(
    ("mutate", "strict", "expected"),
    [
        (lambda r: None, False, 0),
        (lambda r: (r / "skills/demo/SKILL.md").write_bytes(b"x"), False, 0),  # 非关键只告警
        (lambda r: (r / "kernel/agentos-kernel.exe").write_bytes(b"x"), False, 1),  # 关键拦
        (lambda r: _write(r, "skills/legacy/SKILL.md", b"x"), True, 1),  # 严格档多余
    ],
)
def test_cli_verify_exit_codes(tmp_path: Path, mutate, strict: bool, expected: int) -> None:
    _fake_windows_tree(tmp_path)
    manifest_path = tmp_path / "deploy-manifest.json"
    assert mod.main(["build", "--root", str(tmp_path), "--out", str(manifest_path),
                     "--profile", "windows", "--version", "0.2.2"]) == 0
    mutate(tmp_path)
    argv = ["verify", "--root", str(tmp_path), "--manifest", str(manifest_path)]
    if strict:
        argv.append("--strict")
    assert mod.main(argv) == expected


def test_cli_verify_env_error_exit_2(tmp_path: Path) -> None:
    assert mod.main(["verify", "--root", str(tmp_path / "missing"), "--manifest", "x"]) == 2
    assert mod.main(["verify", "--root", str(tmp_path), "--manifest", str(tmp_path / "nope.json")]) == 2


def test_cli_verify_baseline_reports_changed(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _fake_windows_tree(tmp_path)
    old = _build(tmp_path, generated_at="2026-11-09T00:00:00+00:00")
    old_path = tmp_path / "deploy-manifest.prev.json"
    old_path.write_text(json.dumps(old), encoding="utf-8")
    (tmp_path / "skills/demo/SKILL.md").write_bytes(b"# new\n")
    new = _build(tmp_path, generated_at="2026-11-10T00:00:00+00:00")
    new_path = tmp_path / "deploy-manifest.json"
    new_path.write_text(json.dumps(new), encoding="utf-8")

    assert mod.changed_paths(old, new) == ["skills/demo/SKILL.md"]
    assert mod.main(["verify", "--root", str(tmp_path), "--manifest", str(new_path),
                     "--baseline", str(old_path)]) == 0
    out = capsys.readouterr().out
    assert "本次更新变更 1 项" in out and "未落地 0 项" in out


def test_load_manifest_fail_closed(tmp_path: Path) -> None:
    _fake_windows_tree(tmp_path)
    good = _build(tmp_path)
    path = tmp_path / "m.json"

    def _load(payload: dict) -> None:
        path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(mod.ManifestError):
            mod.load_manifest(path)

    assert mod.load_manifest(_write_manifest(tmp_path, good, path))["version"] == "0.2.2"
    _load({**good, "schema": 2})
    _load({**good, "profile": "bogus"})
    _load({**good, "version": ""})
    _load({**good, "build_head": 123})
    _load({**good, "critical": ["not-a-file"]})
    _load({**good, "files": {"a": {"sha256": "short", "bytes": 1}}})
    _load({**good, "files": {"a": {"sha256": "0" * 64, "bytes": -1}}})
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(mod.ManifestError):
        mod.load_manifest(path)


def _write_manifest(root: Path, manifest: dict, path: Path) -> Path:
    mod.write_manifest(path, manifest)
    return path
