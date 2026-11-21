# @feature: FP-0.2.可观测性 dsh_adapter 插件 | @ci: python-coverage
"""官方插件页的 iframe 放行面：端口固定，两处 CSP 声明与 Python 常量机械对账。

契约要点（可观察行为）：

- 官方 Web 应用监听端口必须是**可静态声明**的固定值：前端把它当跨源 iframe
  内嵌，而宿主 CSP（dev = ``frontend/vite.config.ts``、Electron =
  ``electron/app-protocol.ts`` 的 ``APP_CSP``）是构建期/启动期静态声明。
- 两处 CSP 的 ``frame-src`` 必须精确放行该 origin（含端口）；**禁止**出现
  ``http://127.0.0.1:*`` 这类回环通配（那等于把整个回环网段纳入可嵌面）。
- ``frame-src`` 原有基线（``'self' blob: data:``）必须保留：本改动是加白名单，
  不是替换策略。
- 端口 env 覆盖非法时 fail-visible（``port_invalid``），不静默回落——静默回落会
  让 CSP 白名单与真实监听端口脱钩，症状是 iframe 被拦成错误页而日志无声。

为什么这几条重要：端口与 CSP 分处 Python / TS 三个文件，任一处漂移的症状都是
「页面白屏 + 控制台 ERR_BLOCKED_BY_CSP」，无任何链路报错——故用测试把三处钉死。
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from typing import Any

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = PLUGIN_DIR.parents[2]
VITE_CONFIG = REPO_ROOT / "frontend" / "vite.config.ts"
ELECTRON_PROTOCOL = REPO_ROOT / "electron" / "app-protocol.ts"

if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

pytestmark = pytest.mark.unit

_MODULE = "dsh_adapter_web_app_csp_test"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location(_MODULE, PLUGIN_DIR / "web_app.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MODULE] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def wa() -> Any:
    return _load()


def _frame_src_tokens(text: str) -> list[str]:
    """抽出每处 ``frame-src`` 指令的源表达式（到 ``;`` 或引号结束）。

    注释行（``//``、``*``、``#`` 开头）提到 frame-src 属说明文字，不是指令——跳过。
    """
    tokens: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("//", "*", "/*", "#")):
            continue
        if "frame-src" not in stripped:
            continue
        segment = stripped.split("frame-src", 1)[1]
        tokens.append(segment.split(";", 1)[0])
    return tokens


def _assert_frame_src_contract(text: str, port: int, where: str) -> None:
    tokens = _frame_src_tokens(text)
    assert tokens, f"{where} 未声明 frame-src"
    for segment in tokens:
        sources = segment.replace('"', " ").replace("'", " ").split()
        # 基线保留：本改动加白名单，不替换既有可嵌面
        for baseline in ("self", "blob:", "data:"):
            assert baseline in sources, f"{where} 的 frame-src 丢失基线 {baseline}"
        # 精确放行官方页 origin（127.0.0.1 与 localhost 两种回环写法，端口固定）
        assert f"http://127.0.0.1:{port}" in sources, (
            f"{where} 的 frame-src 未放行 http://127.0.0.1:{port}"
        )
        assert f"http://localhost:{port}" in sources, (
            f"{where} 的 frame-src 未放行 http://localhost:{port}"
        )
        # 禁止回环通配：放行面必须收敛到精确端口
        assert "http://127.0.0.1:*" not in segment, f"{where} 使用回环通配，放宽面过大"
        assert "http://localhost:*" not in segment, f"{where} 使用回环通配，放宽面过大"


class TestCspFrameSrc:
    def test_vite_dev_csp_allows_official_origin(self, wa: Any) -> None:
        """dev 前端（vite server.headers）放行官方页 origin。"""
        _assert_frame_src_contract(VITE_CONFIG.read_text(encoding="utf-8"),
                                  wa.WEB_PORT_DEFAULT, "frontend/vite.config.ts")

    def test_electron_csp_allows_official_origin(self, wa: Any) -> None:
        """装机壳（app:// 静态头 APP_CSP）放行官方页 origin。"""
        _assert_frame_src_contract(ELECTRON_PROTOCOL.read_text(encoding="utf-8"),
                                  wa.WEB_PORT_DEFAULT, "electron/app-protocol.ts")

    def test_port_literal_is_not_duplicated_as_magic_number(self, wa: Any) -> None:
        """两处 CSP 用同一端口字面量（改端口只改一处常量 + 两处字面量，可对账）。"""
        port = wa.WEB_PORT_DEFAULT
        assert re.fullmatch(r"\d{4,5}", str(port))
        # 端口不得落在内核/前端固定位（9100/9101 内核，6390 前端 dev）
        assert port not in {9100, 9101, 6390}


class TestWebPort:
    def test_default_port(self, wa: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(wa.WEB_PORT_ENV, raising=False)
        assert wa.web_port() == wa.WEB_PORT_DEFAULT

    def test_env_override(self, wa: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(wa.WEB_PORT_ENV, "9301")
        assert wa.web_port() == 9301

    @pytest.mark.parametrize("raw", ["not-a-number", "80", "70000", "0"])
    def test_invalid_env_is_fail_visible(
        self, wa: Any, monkeypatch: pytest.MonkeyPatch, raw: str,
    ) -> None:
        """非法端口不静默回落：回落会让 CSP 白名单与真实端口脱钩。"""
        monkeypatch.setenv(wa.WEB_PORT_ENV, raw)
        with pytest.raises(wa.WebAppError) as exc:
            wa.web_port()
        assert exc.value.code == "port_invalid"
