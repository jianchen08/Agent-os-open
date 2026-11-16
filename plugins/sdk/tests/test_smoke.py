# @feature: FP-0.2.一 第三方插件协议 | @vision: V3 可嵌入 | @ci: python-test
"""Smoke test — 验证 SDK 包可导入且版本号正确。"""


def test_import_version():
    """验证 agentos_plugin_sdk 可导入且 __version__ 存在。"""
    import tomllib
    from pathlib import Path

    from agentos_plugin_sdk import __version__

    expected = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]["version"]
    assert __version__ == expected
