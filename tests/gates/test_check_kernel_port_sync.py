# @feature: FP-GATE 端口同步闸（整改K4） | @ci: python-coverage
"""check_kernel_port_sync.py 单测（E4 端口常量单源化后的生成一致性执法）。

真值/生成物/闸三方契约（断输入→输出，不钉实现）：
- 落盘生成物 ≡ 按侧风格重渲染（生成幂等）→ 闸绿；
- 任一侧手改（值/host）/生成物缺失 → 闸红（退出 1）并给出再生成指引
  ——fail-closed，闸不允许退化成永绿假闸；
- 随动（本卡验收核心）：生成器真值区改一处 → 两侧生成物同步变化
  （同一真值渲染两侧，改一处两端随动由写盘行为证明）；
- 真值非法（端口越界 / host 带 scheme、路径、空串）→ 渲染期抛错，
  绝不产出坏生成物；
- 负控制（防恒绿）：真实仓生成一致 → 闸子进程实跑退出 0；真值漂移未
  再生成 → 闸必须红。
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
REPO_ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.unit


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


gen = _load("generate_port_constants", SCRIPTS / "generate_port_constants.py")
gate = _load("check_kernel_port_sync", SCRIPTS / "check_kernel_port_sync.py")


def _declaration(content: str) -> str:
    """取生成物中端口声明的唯一匹配，行尾分号归一（两侧风格差异非契约面）。"""
    matches = [line for line in content.splitlines() if line.startswith("export const PACKAGED_KERNEL_PORT")]
    assert len(matches) == 1, matches
    return matches[0].rstrip(";")


# ── 渲染契约：真值 → 两侧声明；确定性 ─────────────────────────────────


@pytest.mark.parametrize(
    ("host", "port"),
    [("127.0.0.1", 9101), ("127.0.0.1", 80), ("127.0.0.1", 65535)],  # 现值 + 低端口边界 + 高端口边界
)
def test_render_declares_truth_on_both_sides(host: str, port: int) -> None:
    for style in ("electron", "frontend"):
        content = gen.render_ts_for(host, port, style)
        assert _declaration(content) == f"export const PACKAGED_KERNEL_PORT = {port}"
        assert "export const PACKAGED_KERNEL_HOST = " in content
        assert host in content


def test_render_deterministic() -> None:
    """性质断言：同真值同输出（无时间戳等扰动），重复渲染零 diff。"""
    assert gen.render_ts_for("127.0.0.1", 9101, "electron") == gen.render_ts_for(
        "127.0.0.1", 9101, "electron"
    )


@pytest.mark.parametrize(
    ("host", "port", "err_fragment"),
    [
        ("127.0.0.1", 0, "端口非法"),
        ("127.0.0.1", 65536, "端口非法"),
        ("127.0.0.1", -1, "端口非法"),
        ("", 9101, "host 非法"),
        ("ws://127.0.0.1", 9101, "host 非法"),
        ("127.0.0.1/x", 9101, "host 非法"),
    ],
)
def test_render_rejects_invalid_truth(host: str, port: int, err_fragment: str) -> None:
    """fail-closed：非法真值拒绝渲染，不产出坏生成物。"""
    with pytest.raises(ValueError, match=err_fragment):
        gen.render_ts_for(host, port, "electron")


# ── 随动（验收核心）：真值一处 → 两侧同步 ─────────────────────────────


@pytest.mark.parametrize(
    ("old_port", "new_port"),
    [(9101, 9102), (9200, 80)],  # 相邻档漂移 + 量级相反换档
)
def test_truth_change_moves_both_generated_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, old_port: int, new_port: int
) -> None:
    electron_ts = tmp_path / "packaged-kernel-port.generated.ts"
    frontend_ts = tmp_path / "packagedKernelPort.generated.ts"

    monkeypatch.setattr(gen, "PACKAGED_KERNEL_PORT", old_port)
    gen.write_outputs(electron_ts, frontend_ts)
    old = (electron_ts.read_text(encoding="utf-8"), frontend_ts.read_text(encoding="utf-8"))

    monkeypatch.setattr(gen, "PACKAGED_KERNEL_PORT", new_port)
    gen.write_outputs(electron_ts, frontend_ts)
    new = (electron_ts.read_text(encoding="utf-8"), frontend_ts.read_text(encoding="utf-8"))

    assert old[0] != new[0], "electron 侧未随真值变化"
    assert old[1] != new[1], "frontend 侧未随真值变化"
    for content in new:
        assert _declaration(content) == f"export const PACKAGED_KERNEL_PORT = {new_port}"


# ── 闸行为：幂等绿 / 漂移红 / 缺失红 ──────────────────────────────────


@pytest.mark.parametrize("port", [9101, 9200])
def test_check_green_after_generate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, port: int) -> None:
    electron_ts = tmp_path / "electron.ts"
    frontend_ts = tmp_path / "frontend.ts"
    monkeypatch.setattr(gen, "PACKAGED_KERNEL_PORT", port)
    gen.write_outputs(electron_ts, frontend_ts)
    assert gen.check_files(electron_ts, frontend_ts) == 0


def test_write_outputs_idempotent(tmp_path: Path) -> None:
    """生成幂等：同真值写两遍零 diff，闸保持绿。"""
    electron_ts = tmp_path / "electron.ts"
    frontend_ts = tmp_path / "frontend.ts"
    gen.write_outputs(electron_ts, frontend_ts)
    first = (electron_ts.read_bytes(), frontend_ts.read_bytes())
    gen.write_outputs(electron_ts, frontend_ts)
    assert (electron_ts.read_bytes(), frontend_ts.read_bytes()) == first
    assert gen.check_files(electron_ts, frontend_ts) == 0


@pytest.mark.parametrize("side", [0, 1])
def test_check_red_on_hand_edited_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, side: int
) -> None:
    paths = [tmp_path / "electron.ts", tmp_path / "frontend.ts"]
    gen.write_outputs(paths[0], paths[1])
    # 从真值派生漂移值（真值换档测试不脆断；不硬编码 9101）
    old_decl = f"= {gen.PACKAGED_KERNEL_PORT}"
    new_decl = f"= {gen.PACKAGED_KERNEL_PORT + 1}"
    tampered = paths[side].read_text(encoding="utf-8").replace(old_decl, new_decl)
    assert tampered != paths[side].read_text(encoding="utf-8"), "漂移值须与真值可区分"
    paths[side].write_text(tampered, encoding="utf-8")
    assert gen.check_files(paths[0], paths[1]) == 1


def test_check_red_on_hand_edited_host(tmp_path: Path) -> None:
    """host 属同刀契约面：手改 host（localhost ≠ 真值回环）同样红。"""
    electron_ts = tmp_path / "electron.ts"
    frontend_ts = tmp_path / "frontend.ts"
    gen.write_outputs(electron_ts, frontend_ts)
    quoted_host = f"'{gen.PACKAGED_KERNEL_HOST}'"
    frontend_ts.write_text(
        frontend_ts.read_text(encoding="utf-8").replace(quoted_host, "'localhost'"),
        encoding="utf-8",
    )
    assert gen.check_files(electron_ts, frontend_ts) == 1


@pytest.mark.parametrize("side", [0, 1])
def test_check_red_on_missing_file(tmp_path: Path, side: int) -> None:
    paths = [tmp_path / "electron.ts", tmp_path / "frontend.ts"]
    gen.write_outputs(paths[0], paths[1])
    paths[side].unlink()
    assert gen.check_files(paths[0], paths[1]) == 1


def test_gate_red_when_truth_drifts_without_regen(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """负控制：改真值忘再生成（落盘仍是旧渲染）→ 闸必须红。"""
    electron_ts = tmp_path / "electron.ts"
    frontend_ts = tmp_path / "frontend.ts"
    gen.write_outputs(electron_ts, frontend_ts)
    drifted = gen.PACKAGED_KERNEL_PORT + 7  # 与现值有区分度的漂移档
    monkeypatch.setattr(gen, "PACKAGED_KERNEL_PORT", drifted)
    assert gen.check_files(electron_ts, frontend_ts) == 1


# ── 真实仓负控制（防恒绿）─────────────────────────────────────────────


def test_real_repo_gate_green_and_generated_header_present() -> None:
    """真实仓状态：闸子进程实跑退出 0；两侧生成物带 DO-NOT-EDIT 头
    （「生成物文件除外」豁免的机械化凭证）。"""
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "check_kernel_port_sync.py")],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    marker = "DO NOT EDIT"
    for path in (gen.ELECTRON_TS, gen.FRONTEND_TS):
        assert marker in path.read_text(encoding="utf-8"), path
