# @feature: FP-0.2.移动端适配 移动构建产物清单 | @ci: python-coverage
"""G-M0/G-M1.3 车道：手机端构建产物 manifest 读写与校验（绿测试）。

被测对象 deploy/artifacts_manifest.py 已落地，本文件是它的行为契约：
- 写面：三产物 digest/字节数/时间戳如实入册（性质断言：入册 digest=重算 digest）；
- 验面：digest 不匹配 / 产物缺失 / 字节数漂移逐项报 problem（拒绝语义由调用方
  承接——CLI 非零退出）；
- schema fail-closed：产物键集、target_arch 词表、字段类型违约一律报错。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from datetime import datetime
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_helper_path = _REPO_ROOT / "deploy" / "artifacts_manifest.py"
_spec = importlib.util.spec_from_file_location("artifacts_manifest", _helper_path)
assert _spec is not None
assert _spec.loader is not None
am = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(am)

pytestmark = pytest.mark.unit


def _seed_artifacts(root: Path, variant: str = "text") -> dict[str, Path]:
    """三产物种子文件；variant 提供有区分度输入（空文件/短文本/跨块大文件）。"""
    contents: dict[str, bytes] = {
        "kernel": b"ELF-fake-kernel-" + variant.encode(),
        "proot": b"proot-binary-" + variant.encode(),
        "rootfs": b"rootfs-tar-" + variant.encode(),
    }
    if variant == "empty":
        contents = dict.fromkeys(contents, b"")
    elif variant == "large":
        # >1MB：跨过 helper 的 1MB 分块读边界，验证分块 digest 与一次性等价
        contents = {k: bytes(k.encode()[0]) * (2 * 1024 * 1024 + 7) for k in contents}
    paths: dict[str, Path] = {}
    for name, data in contents.items():
        p = root / "artifacts" / f"{name}.bin"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        paths[name] = p
    return paths


class TestWriteManifest:
    def test_roundtrip_write_and_verify_pass(self, tmp_path: Path) -> None:
        """写后即验通过；入册 digest/bytes 与实物重算一致（性质断言）。"""
        artifacts = _seed_artifacts(tmp_path, variant="text")
        out = tmp_path / "artifacts_manifest.json"

        manifest = am.write_manifest(out, artifacts, "aarch64", "0.2.0-m1")

        assert am.verify_manifest(out) == []
        assert set(manifest["artifacts"]) == {"kernel", "proot", "rootfs"}
        for name, entry in manifest["artifacts"].items():
            assert entry["sha256"] == hashlib.sha256(artifacts[name].read_bytes()).hexdigest()
            assert entry["bytes"] == artifacts[name].stat().st_size

    @pytest.mark.parametrize("arch", ["aarch64", "x86_64"])
    def test_both_arch_variants_accepted(self, tmp_path: Path, arch: str) -> None:
        """换元两档（aarch64/x86_64）写面均可用且如实入册。"""
        artifacts = _seed_artifacts(tmp_path / arch, variant="text")
        out = tmp_path / arch / "artifacts_manifest.json"

        manifest = am.write_manifest(out, artifacts, arch, "1.0.0")

        assert manifest["target_arch"] == arch
        assert am.verify_manifest(out) == []

    @pytest.mark.parametrize("variant", ["empty", "large"])
    def test_content_variants_digest_stable(self, tmp_path: Path, variant: str) -> None:
        """空文件与跨块大文件：digest 计算对内容规模不敏感（分块=一次性）。"""
        artifacts = _seed_artifacts(tmp_path / variant, variant=variant)
        out = tmp_path / variant / "artifacts_manifest.json"

        manifest = am.write_manifest(out, artifacts, "aarch64", "1.0.0")

        for name, entry in manifest["artifacts"].items():
            assert entry["sha256"] == hashlib.sha256(artifacts[name].read_bytes()).hexdigest()
        assert am.verify_manifest(out) == []

    def test_built_at_is_tz_aware_iso8601(self, tmp_path: Path) -> None:
        """built_at 必须是带时区的 ISO8601（跨端排序与追溯的前提）。"""
        artifacts = _seed_artifacts(tmp_path, variant="text")

        manifest = am.build_manifest(artifacts, "aarch64", "1.0.0")

        parsed = datetime.fromisoformat(manifest["built_at"])
        assert parsed.tzinfo is not None

    def test_injected_built_at_respected(self, tmp_path: Path) -> None:
        artifacts = _seed_artifacts(tmp_path, variant="text")

        manifest = am.build_manifest(
            artifacts, "aarch64", "1.0.0", built_at="2026-10-13T00:00:00+00:00"
        )

        assert manifest["built_at"] == "2026-10-13T00:00:00+00:00"


class TestWriteManifestFailClosed:
    @pytest.mark.parametrize(
        ("mutate", "fragment"),
        [
            (lambda a: a.pop("rootfs"), "缺一不可"),
            (lambda a: a.update(extra=a["kernel"]), "缺一不可"),
        ],
    )
    def test_artifact_key_set_is_exact(
        self, tmp_path: Path, mutate, fragment: str
    ) -> None:
        """产物键集必须恰为三件：缺件/多件（含重命名）一律拒绝。"""
        artifacts = _seed_artifacts(tmp_path, variant="text")
        mutate(artifacts)

        with pytest.raises(am.ManifestError, match=fragment):
            am.build_manifest(artifacts, "aarch64", "1.0.0")

    def test_missing_artifact_file_rejected(self, tmp_path: Path) -> None:
        artifacts = _seed_artifacts(tmp_path, variant="text")
        artifacts["kernel"].unlink()

        with pytest.raises(am.ManifestError, match="产物不存在: kernel"):
            am.build_manifest(artifacts, "aarch64", "1.0.0")

    @pytest.mark.parametrize("arch", ["arm64", "amd64", ""])
    def test_unknown_arch_rejected(self, tmp_path: Path, arch: str) -> None:
        """架构词表外（含别名大小写差异语义，如 arm64≠aarch64）fail-closed。"""
        artifacts = _seed_artifacts(tmp_path, variant="text")

        with pytest.raises(am.ManifestError, match="target_arch"):
            am.build_manifest(artifacts, arch, "1.0.0")

    def test_empty_version_rejected(self, tmp_path: Path) -> None:
        artifacts = _seed_artifacts(tmp_path, variant="text")

        with pytest.raises(am.ManifestError, match="version"):
            am.build_manifest(artifacts, "aarch64", "")


class TestVerifyManifest:
    def test_digest_mismatch_reported(self, tmp_path: Path) -> None:
        """digest 不匹配=验面拒绝（逐项报 problem，拒绝动作归调用方）。"""
        artifacts = _seed_artifacts(tmp_path, variant="text")
        out = tmp_path / "artifacts_manifest.json"
        am.write_manifest(out, artifacts, "aarch64", "1.0.0")
        artifacts["kernel"].write_bytes(b"tampered-kernel")

        problems = am.verify_manifest(out)

        assert len(problems) == 1
        assert problems[0].startswith("kernel: digest 不匹配")
        assert "实际=" in problems[0]

    def test_missing_file_reported_without_masking(self, tmp_path: Path) -> None:
        """产物缺失单独成 problem，且不伪装成 digest 问题。"""
        artifacts = _seed_artifacts(tmp_path, variant="text")
        out = tmp_path / "artifacts_manifest.json"
        am.write_manifest(out, artifacts, "aarch64", "1.0.0")
        artifacts["rootfs"].unlink()

        problems = am.verify_manifest(out)

        assert problems == ["rootfs: 产物文件缺失: " + str(artifacts["rootfs"])]

    def test_all_problems_reported_together(self, tmp_path: Path) -> None:
        """多产物同时违约时逐项全报，不做首错短路（一次验面看全貌）。"""
        artifacts = _seed_artifacts(tmp_path, variant="text")
        out = tmp_path / "artifacts_manifest.json"
        am.write_manifest(out, artifacts, "aarch64", "1.0.0")
        artifacts["kernel"].write_bytes(b"x")
        artifacts["proot"].unlink()

        problems = am.verify_manifest(out)

        assert [p.split(":")[0] for p in problems] == ["kernel", "proot"]

    def test_relative_paths_resolve_against_manifest_dir(self, tmp_path: Path) -> None:
        """相对路径默认相对 manifest 所在目录解析（不在 cwd 也不在仓库根）。"""
        artifacts = _seed_artifacts(tmp_path, variant="text")
        out = tmp_path / "sub" / "artifacts_manifest.json"
        manifest = am.build_manifest(artifacts, "aarch64", "1.0.0")
        for entry in manifest["artifacts"].values():
            entry["path"] = f"artifacts/{Path(entry['path']).name}"
        out.parent.mkdir(parents=True)
        out.write_text(json.dumps(manifest), encoding="utf-8")

        # 相对 manifest 目录（tmp_path/sub/）解析 → 缺件
        problems = am.verify_manifest(out)
        assert problems
        assert all("产物文件缺失" in p for p in problems)

        # base_dir 显式覆盖回真实产物目录 → 通过
        assert am.verify_manifest(out, base_dir_override=tmp_path) == []

    def test_absolute_path_artifact_verified(self, tmp_path: Path) -> None:
        """绝对路径产物不参与相对解析，直接验实物。"""
        artifacts = _seed_artifacts(tmp_path, variant="text")
        out = tmp_path / "nested" / "artifacts_manifest.json"
        am.write_manifest(out, artifacts, "aarch64", "1.0.0")

        assert am.verify_manifest(out) == []


class TestLoadManifestFailClosed:
    def _valid_manifest(self, tmp_path: Path) -> tuple[Path, dict]:
        artifacts = _seed_artifacts(tmp_path, variant="text")
        out = tmp_path / "artifacts_manifest.json"
        manifest = am.write_manifest(out, artifacts, "aarch64", "1.0.0")
        return out, manifest

    @pytest.mark.parametrize(
        ("mutate", "fragment"),
        [
            (lambda m: m.pop("version"), "缺必填字段"),
            (lambda m: m.update(schema_version=99), "schema_version"),
            (lambda m: m.update(target_arch="armv7"), "target_arch"),
            (lambda m: m.update(version=""), "version"),
            (lambda m: m["artifacts"].update(bootloader=m["artifacts"]["kernel"]), "缺一不可"),
            (lambda m: m["artifacts"]["kernel"].pop("sha256"), "缺字段"),
            (lambda m: m["artifacts"]["kernel"].update(sha256="deadbeef"), "sha256"),
            (lambda m: m["artifacts"]["kernel"].update(bytes=-1), "bytes"),
            (lambda m: m["artifacts"]["kernel"].update(path=""), "path"),
        ],
    )
    def test_schema_violations_rejected(self, tmp_path: Path, mutate, fragment: str) -> None:
        """读面 fail-closed：字段缺失/词表外/类型违约逐项报错，不静默放行。"""
        out, manifest = self._valid_manifest(tmp_path)
        mutate(manifest)
        out.write_text(json.dumps(manifest), encoding="utf-8")

        with pytest.raises(am.ManifestError, match=fragment):
            am.load_manifest(out)


class TestCli:
    def test_write_then_verify_cli_roundtrip(self, tmp_path: Path) -> None:
        artifacts = _seed_artifacts(tmp_path, variant="text")
        out = tmp_path / "artifacts_manifest.json"

        rc = am._cli(
            [
                "write",
                "--out",
                str(out),
                "--arch",
                "x86_64",
                "--version",
                "1.0.0",
                *[f"--artifact={k}={v}" for k, v in artifacts.items()],
            ]
        )
        assert rc == 0
        assert am.verify_manifest(out) == []

        rc = am._cli(["verify", "--manifest", str(out)])
        assert rc == 0

    def test_verify_cli_nonzero_on_mismatch(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        artifacts = _seed_artifacts(tmp_path, variant="text")
        out = tmp_path / "artifacts_manifest.json"
        am.write_manifest(out, artifacts, "aarch64", "1.0.0")
        artifacts["proot"].write_bytes(b"tampered")

        rc = am._cli(["verify", "--manifest", str(out)])

        assert rc == 1
        assert "digest 不匹配" in capsys.readouterr().err

    def test_write_cli_rejects_bad_key(self, tmp_path: Path) -> None:
        artifacts = _seed_artifacts(tmp_path, variant="text")

        with pytest.raises(SystemExit):
            am._cli(
                [
                    "write",
                    "--out",
                    str(tmp_path / "m.json"),
                    "--arch",
                    "aarch64",
                    "--version",
                    "1.0.0",
                    f"--artifact=bogus={artifacts['kernel']}",
                ]
            )

    def test_write_cli_rejects_duplicate_key(self, tmp_path: Path) -> None:
        artifacts = _seed_artifacts(tmp_path, variant="text")

        with pytest.raises(SystemExit):
            am._cli(
                [
                    "write",
                    "--out",
                    str(tmp_path / "m.json"),
                    "--arch",
                    "aarch64",
                    "--version",
                    "1.0.0",
                    f"--artifact=kernel={artifacts['kernel']}",
                    f"--artifact=kernel={artifacts['proot']}",
                ]
            )
