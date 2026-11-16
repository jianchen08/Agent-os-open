# @feature: FP-0.2.CFG 配置系统 | @ci: python-coverage
"""diagnostics_settings 公共件测试（用户空间 settings.yaml 诊断开关）。

行为契约（ADR 2026-10-10-diagnostics-settings-yaml）：
- 优先级：settings.yaml diagnostics 段 > DEFAULTS；无文件 = 全默认
- 坏 yaml / 坏段 / 弱真值（字符串 "true"、整数 1）回落默认（bool 严格判定）
- mtime 缓存：文件变更后下一次 read 即新值（热读契约）
- write：合并写回（保留未知段与键）、未知键拒绝、非 bool 拒绝、原子替换
- user_root：AGENTOS_USER_ROOT env 显式覆盖 > OS 标准目录
"""

from __future__ import annotations

import pytest

from agentos_plugin_sdk import diagnostics_settings as ds

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def sandbox_user_root(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """钉 AGENTOS_USER_ROOT 到 tmp 并清缓存——不触真实用户空间。"""
    monkeypatch.setenv("AGENTOS_USER_ROOT", str(tmp_path))
    ds._cache.clear()
    yield
    ds._cache.clear()


def test_defaults_without_file(tmp_path) -> None:
    """无 settings.yaml → 全默认（零操作前提：不改任何东西即正确态）。"""
    assert ds.read_diagnostics() == {"payload_diag": True, "prompt_audit": False}
    assert not (tmp_path / "settings.yaml").exists()


def test_reads_written_values(tmp_path) -> None:
    """diagnostics 段键值 → 覆盖默认（bool 才认）。"""
    (tmp_path / "settings.yaml").write_text(
        "diagnostics:\n  payload_diag: false\n  prompt_audit: true\n", encoding="utf-8"
    )
    assert ds.read_diagnostics() == {"payload_diag": False, "prompt_audit": True}


@pytest.mark.parametrize(
    "content",
    [
        "diagnostics: [unclosed",  # 坏 yaml（半截写入形态）
        "diagnostics: not-a-mapping\n",  # 段非 dict
        "diagnostics:\n  payload_diag: 'true'\n",  # 字符串弱真值不认
        "diagnostics:\n  payload_diag: 1\n",  # 整数弱真值不认
        "diagnostics:\n  unknown_key: true\n",  # 未知键忽略（键面单点登记）
    ],
)
def test_malformed_falls_back_to_defaults(tmp_path, content: str) -> None:
    """坏形态逐类回落默认（fail-open 与默认一致，不阻断调用方主路径）。"""
    (tmp_path / "settings.yaml").write_text(content, encoding="utf-8")
    assert ds.read_diagnostics() == dict(ds.DEFAULTS)


def test_mtime_cache_hot_reload(tmp_path) -> None:
    """缓存热读：文件改后下一次 read 即新值（同一进程内）。"""
    first = ds.read_diagnostics()
    assert first["payload_diag"] is True
    (tmp_path / "settings.yaml").write_text("diagnostics:\n  payload_diag: false\n", encoding="utf-8")
    assert ds.read_diagnostics()["payload_diag"] is False


def test_read_returns_independent_copy(tmp_path) -> None:
    """read 返回副本：调用方改返回值不污染缓存。"""
    result = ds.read_diagnostics()
    result["payload_diag"] = False
    assert ds.read_diagnostics()["payload_diag"] is True


def test_write_merges_and_preserves_unknown_sections(tmp_path) -> None:
    """write 只动 diagnostics 段：其余段与段内未知键原样保留。"""
    settings = tmp_path / "settings.yaml"
    settings.write_text(
        "logging:\n  level: debug\ndiagnostics:\n  custom_future_key: true\n",
        encoding="utf-8",
    )
    result = ds.write_diagnostics({"payload_diag": False})
    assert result == {"payload_diag": False, "prompt_audit": False}
    text = settings.read_text(encoding="utf-8")
    assert "level: debug" in text  # 未知段保留
    assert "custom_future_key" in text  # 段内未知键保留（前向兼容）
    # 写后 read 即新值（缓存已失效）
    assert ds.read_diagnostics()["payload_diag"] is False


def test_write_creates_file_and_dirs(tmp_path) -> None:
    """无既有文件 → write 创建（含父目录），首次写不要求文件先存在。"""
    monkeypatch_root = tmp_path / "nested" / "root"
    import os

    os.environ["AGENTOS_USER_ROOT"] = str(monkeypatch_root)
    ds._cache.clear()
    try:
        result = ds.write_diagnostics({"prompt_audit": True})
        assert result["prompt_audit"] is True
        assert (monkeypatch_root / "settings.yaml").exists()
    finally:
        os.environ["AGENTOS_USER_ROOT"] = str(tmp_path)


def test_write_rejects_unknown_key_and_non_bool(tmp_path) -> None:
    """未知键拒绝（键面单点登记）、非 bool 值拒绝——fail-closed。"""
    with pytest.raises(ValueError, match="未知诊断开关键"):
        ds.write_diagnostics({"brand_new_switch": True})
    with pytest.raises(ValueError, match="须为 bool"):
        ds.write_diagnostics({"payload_diag": "false"})
    assert not (tmp_path / "settings.yaml").exists()  # 拒绝时零写入


def test_user_root_env_override(tmp_path) -> None:
    """AGENTOS_USER_ROOT 显式覆盖优先于 OS 标准目录。"""
    assert ds.settings_path() == tmp_path / "settings.yaml"


def test_defaults_are_readonly() -> None:
    """DEFAULTS 是只读视图：误改即抛（键面唯一注册处防污染）。"""
    with pytest.raises(TypeError):
        ds.DEFAULTS["payload_diag"] = False  # type: ignore[index]
