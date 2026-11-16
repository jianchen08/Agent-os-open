# @feature: FP-0.2.二 模式体系收口批 B3 | @ci: python-coverage
"""retire_mode_planning_seeds 安全闸测试（P4 一次性退役脚本）。

对账语义与 kernel user_space reconcile 同源：账本清单内任一文件哈希不一致/
缺失 = 已定制；已定制而未 ``--force`` 必须拒删（打印差异清单，退出码 2）。
文件面一律真实临时目录 + 真实 sha256，不 mock。
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "retire_mode_planning_seeds.py"
_spec = importlib.util.spec_from_file_location("retire_mode_planning_seeds", _SCRIPT)
assert _spec and _spec.loader
retire = importlib.util.module_from_spec(_spec)
sys.modules["retire_mode_planning_seeds"] = retire
_spec.loader.exec_module(retire)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


_COPY_FILES = {
    "plugin.json": b'{"id": "mode_planning", "version": "1.2.0"}',
    "mode.yaml": b"mode: planning\nname: planned\n",
}


def _mk_user_root(
    tmp_path: Path, *, with_copy: bool = True, with_ledger: bool = True, tamper: bool = False
) -> Path:
    """构造用户空间：副本（可定制化）+ 账本（含他包条目，验证不受伤）。"""
    root = tmp_path / "user_root"
    modes = root / "plugins" / "modes"
    if with_copy:
        copy_dir = modes / "mode_planning"
        copy_dir.mkdir(parents=True)
        for name, body in _COPY_FILES.items():
            (copy_dir / name).write_bytes(body)
    if with_ledger:
        files = {name: _sha(body) for name, body in _COPY_FILES.items()}
        if tamper:
            # 账本基线停在旧代：mode.yaml 哈希与副本不一致（= 已定制）
            files["mode.yaml"] = _sha(b"mode: planning\nname: stale-baseline\n")
        (modes / ".seeds.json").write_text(
            json.dumps(
                {
                    "mode_planning": {"seeded_version": "1.2.0", "files": files},
                    "mode_coding": {"seeded_version": "1.3.0", "files": {"keep.txt": "h"}},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    return root


def _ledger(root: Path) -> dict:
    return json.loads((root / "plugins" / "modes" / ".seeds.json").read_text(encoding="utf-8"))


def test_clean_copy_retires_copy_and_ledger_entry_only(tmp_path: Path) -> None:
    root = _mk_user_root(tmp_path)
    assert retire.main(["--user-root", str(root)]) == 0
    assert not (root / "plugins" / "modes" / "mode_planning").exists()
    ledger = _ledger(root)
    assert "mode_planning" not in ledger
    assert ledger["mode_coding"]["seeded_version"] == "1.3.0"  # 他包条目不受伤


def test_customized_copy_held_without_force(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    root = _mk_user_root(tmp_path, tamper=True)
    assert retire.main(["--user-root", str(root)]) == 2
    # 差异清单点名被改文件；副本与账本原样保留
    out = capsys.readouterr().out
    assert "[已改] mode.yaml" in out
    assert (root / "plugins" / "modes" / "mode_planning" / "mode.yaml").exists()
    assert "mode_planning" in _ledger(root)


def test_customized_copy_force_retires(tmp_path: Path) -> None:
    root = _mk_user_root(tmp_path, tamper=True)
    assert retire.main(["--user-root", str(root), "--force"]) == 0
    assert not (root / "plugins" / "modes" / "mode_planning").exists()
    assert "mode_planning" not in _ledger(root)


def test_dry_run_reports_without_touching(tmp_path: Path) -> None:
    root = _mk_user_root(tmp_path)
    assert retire.main(["--user-root", str(root), "--dry-run"]) == 0
    assert (root / "plugins" / "modes" / "mode_planning" / "plugin.json").exists()
    assert "mode_planning" in _ledger(root)


def test_manual_copy_without_ledger_requires_force(tmp_path: Path) -> None:
    """手工放置副本（账本无条目，无基线可比）视同已定制：拒删 → --force 放行。"""
    root = _mk_user_root(tmp_path, with_ledger=False)
    assert retire.main(["--user-root", str(root)]) == 2
    assert (root / "plugins" / "modes" / "mode_planning").exists()
    assert retire.main(["--user-root", str(root), "--force"]) == 0
    assert not (root / "plugins" / "modes" / "mode_planning").exists()


def test_absent_copy_and_ledger_is_noop(tmp_path: Path) -> None:
    root = tmp_path / "fresh_root"
    root.mkdir()
    assert retire.main(["--user-root", str(root)]) == 0
