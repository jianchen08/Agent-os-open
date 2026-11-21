# @feature: FP-0.2.PACK MXC 运行时随包 | @ci: python-coverage
"""MXC 运行时取件脚本单测（zip 中央目录解析 / Range 解压 / 校验 / CLI）。

网络不触：合成 zip 字节直接喂解析器；``_http_range`` 用替身按切片回放。
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import sys
import zipfile
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _ROOT / "scripts" / "fetch_mxc_runtime.py"


def _load_mod() -> Any:
    name = "fetch_mxc_runtime_test_mod"
    if name in sys.modules:
        del sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_MOD = _load_mod()


def _zip_bytes(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)
    return buf.getvalue()


class TestCentralDirectory:
    def test_parse_members_and_offsets(self) -> None:
        data = _zip_bytes({"x64/a.exe": b"hello-a", "x64/b.dll": b"hello-b"})
        cd_off, cd_size, n_total, entries = _MOD._parse_central_directory(data, 0)
        assert n_total == 2
        assert cd_off + cd_size <= len(data)
        assert [entry["name"] for entry in entries] == ["x64/a.exe", "x64/b.dll"]
        assert entries[0]["usize"] == len(b"hello-a")

    def test_parse_entries_from_cd_slice(self) -> None:
        data = _zip_bytes({"x64/a.exe": b"hello-a"})
        cd_off, cd_size, n_total, _ = _MOD._parse_central_directory(data, 0)
        entries = _MOD._parse_entries(data[cd_off : cd_off + cd_size], n_total)
        assert entries[0]["name"] == "x64/a.exe"

    def test_missing_eocd_rejected(self) -> None:
        with pytest.raises(_MOD.FetchError):
            _MOD._parse_central_directory(b"not-a-zip", 0)


class TestExtractMember:
    def _entry(self, data: bytes, name: str) -> dict[str, Any]:
        _, _, _, entries = _MOD._parse_central_directory(data, 0)
        entry = next(item for item in entries if item["name"] == name)
        payload = b"hello-a"
        return {**entry, "sha256": hashlib.sha256(payload).hexdigest()}

    def test_extract_verified_member(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        data = _zip_bytes({"x64/a.exe": b"hello-a"})
        monkeypatch.setattr(
            _MOD,
            "_http_range",
            lambda url, start, end, *, token=None: data[start : end + 1],  # noqa: ARG005
        )
        dest = tmp_path / "x64" / "a.exe"
        _MOD._extract_member("https://example.invalid", self._entry(data, "x64/a.exe"), dest, token=None)
        assert dest.read_bytes() == b"hello-a"

    def test_sha_mismatch_rejected(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        data = _zip_bytes({"x64/a.exe": b"hello-a"})
        monkeypatch.setattr(
            _MOD,
            "_http_range",
            lambda url, start, end, *, token=None: data[start : end + 1],  # noqa: ARG005
        )
        entry = {**self._entry(data, "x64/a.exe"), "sha256": "0" * 64}
        with pytest.raises(_MOD.FetchError, match="SHA-256"):
            _MOD._extract_member(
                "https://example.invalid", entry, tmp_path / "a.exe", token=None
            )


class TestVerifyOutput:
    def _manifest(self, payload: bytes, path: str = "x64/a.exe") -> dict[str, Any]:
        return {
            "members": [
                {
                    "path": path,
                    "size": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                }
            ]
        }

    def test_complete_and_tampered(self, tmp_path: Path) -> None:
        payload = b"payload"
        manifest = self._manifest(payload)
        (tmp_path / "x64").mkdir()
        (tmp_path / "x64" / "a.exe").write_bytes(payload)
        assert _MOD._verify_output(tmp_path, manifest) == []
        (tmp_path / "x64" / "a.exe").write_bytes(b"PAYLOAD")
        problems = _MOD._verify_output(tmp_path, manifest)
        assert problems
        assert "SHA-256" in problems[0]

    def test_missing_member(self, tmp_path: Path) -> None:
        problems = _MOD._verify_output(tmp_path, self._manifest(b"x"))
        assert problems
        assert "缺少" in problems[0]


class TestCliCheck:
    def test_check_fails_on_missing_output(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        manifest = tmp_path / "manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "members": [
                        {"path": "x64/a.exe", "size": 1, "sha256": "0" * 64}
                    ]
                }
            ),
            encoding="utf-8",
        )
        code = _MOD.main(
            ["--check", "--manifest", str(manifest), "--out", str(tmp_path / "out")]
        )
        assert code == 1
        assert "校验失败" in capsys.readouterr().out

    def test_check_passes_on_complete_output(
        self, tmp_path: Path, capsys: Any
    ) -> None:
        payload = b"x"
        out = tmp_path / "out"
        (out / "x64").mkdir(parents=True)
        (out / "x64" / "a.exe").write_bytes(payload)
        manifest = tmp_path / "manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "members": [
                        {
                            "path": "x64/a.exe",
                            "size": 1,
                            "sha256": hashlib.sha256(payload).hexdigest(),
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        assert (
            _MOD.main(["--check", "--manifest", str(manifest), "--out", str(out)]) == 0
        )
        assert "校验通过" in capsys.readouterr().out
