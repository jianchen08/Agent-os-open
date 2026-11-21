# R15：isolation 插件测试数据根隔离（conftest 作用域 = 本插件测试目录）。
# 外部绑定注册表持久化（manager._save/_load_external_bindings_*）落
# AGENTOS_DATA_DIR——不钉桩会写进真实用户空间数据目录（测试副作用泄漏）。
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTOS_DATA_DIR", str(tmp_path / "data"))
