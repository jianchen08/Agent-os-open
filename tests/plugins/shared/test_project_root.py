# @feature: FP-0.2.整改F-2 根解析单源 | @ci: python-coverage
"""project_root 解析单源测试（整改 F-2）。

三步语义逐面验证：env 优先（两种合法形态）→ 向上探测（锚点含自身）→
(指纹, 锚点) 缓存与 reset_cache；指纹差异参数化（≥2 指纹同锚不同结果）；
fallback 不进缓存；空指纹契约错误。
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SHARED_ROOT = _REPO_ROOT / "plugins" / "shared"
if str(_SHARED_ROOT) not in sys.path:
    sys.path.insert(0, str(_SHARED_ROOT))

import project_root  # noqa: E402
from project_root import resolve_project_root  # noqa: E402

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clean_cache() -> Iterator[None]:
    """每测前后清缓存（测试改 env 后必须重置，见 reset_cache 契约）。"""
    project_root.reset_cache()
    yield
    project_root.reset_cache()


def _make_root(base: Path, name: str, *fingerprint: str) -> Path:
    """造一个满足指纹的假项目根（指纹部件为相对目录标记）。"""
    root = base / name
    for part in fingerprint:
        (root / part).mkdir(parents=True, exist_ok=True)
    return root


# ── env 优先：两种合法形态（<根>/config 与根本身）同根 ──────────────────
@pytest.mark.parametrize("env_form", ["config_dir", "root_itself"])
def test_env_priority_both_legal_forms_hit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, env_form: str
) -> None:
    """AGENTOS_CONFIG_ROOT 指向 <根>/config 或根本身，皆解析为该根。"""
    root = _make_root(tmp_path, "root", "config", "config/kernel")
    env = str(root / "config") if env_form == "config_dir" else str(root)
    monkeypatch.setenv("AGENTOS_CONFIG_ROOT", env)
    got = resolve_project_root("config", "config/kernel", start=tmp_path / "nowhere")
    assert got == root


@pytest.mark.parametrize(
    "fingerprint",
    [
        ("config", "config/kernel"),
        ("config",),
        ("config/kernel",),
        ("config/models",),
    ],
)
def test_env_hit_per_fingerprint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fingerprint: tuple[str, ...]
) -> None:
    """指纹参数化：env 根满足全部标记才采用（四种现役指纹逐一）。"""
    root = tmp_path / "root"
    for part in fingerprint:
        (root / part).mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("AGENTOS_CONFIG_ROOT", str(root / "config"))
    assert resolve_project_root(*fingerprint, start=tmp_path) == root


def test_env_root_missing_partial_marker_not_adopted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """指纹须全部满足：只含 config 不含 config/kernel 的目录不被
    ("config","config/kernel") 指纹采用（env 指向别处不能劫持解析）。"""
    bogus = _make_root(tmp_path, "bogus", "config")
    probe_root = _make_root(tmp_path, "real", "config", "config/kernel")
    anchor = probe_root / "plugins" / "shared"
    anchor.mkdir(parents=True)
    monkeypatch.setenv("AGENTOS_CONFIG_ROOT", str(bogus / "config"))
    assert resolve_project_root("config", "config/kernel", start=anchor) == probe_root


# ── 探测回落：env 不中/缺失时自锚点向上 ─────────────────────────────────
def test_env_absent_falls_back_to_probe(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """env 缺失 → 自锚点（含自身）向上探测第一个含全部标记的祖先。"""
    root = _make_root(tmp_path, "root", "config", "config/kernel")
    anchor = root / "plugins" / "shared"
    anchor.mkdir(parents=True)
    monkeypatch.delenv("AGENTOS_CONFIG_ROOT", raising=False)
    assert resolve_project_root("config", "config/kernel", start=anchor) == root


def test_probe_includes_start_itself(tmp_path: Path) -> None:
    """探测含锚点自身：锚点即根时直接命中（不向上多走一层）。"""
    root = _make_root(tmp_path, "root", "config")
    assert resolve_project_root("config", start=root) == root


def test_no_match_without_fallback_returns_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """env 缺失 + 祖先链无指纹标记 → None（消费方自持兜底语义）。"""
    anchor = tmp_path / "a" / "b" / "c"
    anchor.mkdir(parents=True)
    monkeypatch.delenv("AGENTOS_CONFIG_ROOT", raising=False)
    assert resolve_project_root("config", "config/kernel", start=anchor) is None


# ── 指纹差异：同锚不同指纹结论独立 ───────────────────────────────────────
def test_fingerprint_difference_changes_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """同锚两指纹不同结果：近祖只有 config/（("config",) 命中），
    ("config","config/kernel") 越过它命中更上的全指纹根——指纹差异参数化
    的区分度证据。"""
    outer = _make_root(tmp_path, "outer", "config", "config/kernel")
    inner = _make_root(outer, "inner", "config")
    anchor = inner / "deep"
    anchor.mkdir()
    monkeypatch.delenv("AGENTOS_CONFIG_ROOT", raising=False)
    assert resolve_project_root("config", start=anchor) == inner
    assert resolve_project_root("config", "config/kernel", start=anchor) == outer


def test_cache_isolated_per_fingerprint_and_anchor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """缓存按 (指纹, 锚点) 隔离：同指纹不同锚点各自解析，互不串扰。"""
    monkeypatch.delenv("AGENTOS_CONFIG_ROOT", raising=False)
    root_a = _make_root(tmp_path, "a", "config")
    root_b = _make_root(tmp_path, "b", "config")
    assert resolve_project_root("config", start=root_a) == root_a
    assert resolve_project_root("config", start=root_b) == root_b


# ── 缓存语义：命中不重解析；reset_cache 后随 env 重解析 ─────────────────
def test_cache_hit_freezes_result_across_env_change(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """缓存命中性质：同 (指纹, 锚点) 二次调用不重解析——env 变更不可见。"""
    root = _make_root(tmp_path, "root", "config")
    monkeypatch.setenv("AGENTOS_CONFIG_ROOT", str(root / "config"))
    anchor = tmp_path / "anchor"
    anchor.mkdir()
    assert resolve_project_root("config", start=anchor) == root
    other = _make_root(tmp_path, "other", "config")
    monkeypatch.setenv("AGENTOS_CONFIG_ROOT", str(other / "config"))
    assert resolve_project_root("config", start=anchor) == root, "缓存期内 env 变更不重解析"


def test_reset_cache_re_resolves_with_new_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """reset_cache 后按新 env 重解析（测试改环境后的复位契约）。"""
    root1 = _make_root(tmp_path, "root1", "config")
    root2 = _make_root(tmp_path, "root2", "config")
    anchor = tmp_path / "anchor"
    anchor.mkdir()
    monkeypatch.setenv("AGENTOS_CONFIG_ROOT", str(root1 / "config"))
    assert resolve_project_root("config", start=anchor) == root1
    monkeypatch.setenv("AGENTOS_CONFIG_ROOT", str(root2 / "config"))
    project_root.reset_cache()
    assert resolve_project_root("config", start=anchor) == root2


# ── fallback：不进缓存 ────────────────────────────────────────────────────
def test_fallback_returned_on_miss_and_not_cached(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """探测不中 → 返回 fallback；fallback 不进缓存——同键换 fallback 可见。"""
    anchor = tmp_path / "a" / "b"
    anchor.mkdir(parents=True)
    monkeypatch.delenv("AGENTOS_CONFIG_ROOT", raising=False)
    fb1, fb2 = tmp_path / "fb1", tmp_path / "fb2"
    assert resolve_project_root("config", start=anchor, fallback=fb1) == fb1
    assert resolve_project_root("config", start=anchor, fallback=fb2) == fb2
    assert resolve_project_root("config", start=anchor) is None


# ── 契约错误 ──────────────────────────────────────────────────────────────
def test_empty_fingerprint_raises(tmp_path: Path) -> None:
    """空指纹是调用方契约错误（ValueError）。"""
    with pytest.raises(ValueError, match="指纹"):
        resolve_project_root(start=tmp_path)


# ── 迁移零变化锚：四种现役消费指纹对本仓布局的解析与仓根一致 ──────────────
@pytest.mark.parametrize(
    "fingerprint",
    [
        ("config", "config/kernel"),
        ("config",),
        ("config/kernel",),
        ("config/models",),
    ],
)
def test_repo_layout_resolution_matches_repo_root(
    monkeypatch: pytest.MonkeyPatch, fingerprint: tuple[str, ...]
) -> None:
    """本仓真实布局：四种现役指纹自 plugins/shared 锚点均解析到仓根
    （消费方迁移前后解析结果零变化的仓内回归锚）。"""
    monkeypatch.delenv("AGENTOS_CONFIG_ROOT", raising=False)
    got = resolve_project_root(*fingerprint, start=_SHARED_ROOT)
    assert got == _REPO_ROOT
    for part in fingerprint:
        assert (got / part).is_dir(), "结果必须满足指纹全标记（性质断言）"
