"""Smoke test — 验证 SDK 包可导入且版本号正确。"""


def test_import_version():
    """验证 agentos_plugin_sdk 可导入且 __version__ 存在。"""
    from agentos_plugin_sdk import __version__

    assert __version__ == "0.2.0"
