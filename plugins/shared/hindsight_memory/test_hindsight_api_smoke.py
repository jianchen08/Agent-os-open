# @feature: FP-0.2.六 记忆检索 | @vision: V1 可进化 | @ci: none-local
"""hindsight-api 真启动冒烟（重型豁免套件，coverage_exempt.EXEMPT_SUITES 登记）。

覆盖单元 mock 面之外的真实启动契约：以真实 aux venv 解释器 spawn hindsight-api
子进程（stdout/stderr 双 PIPE + Job 钉死 + 双流排空，与生产 _start_api_server
全同款），断言 /health 200。真实子进程 × 真实环境层的历史事故——GBK 代码页横幅
秒死（装机 2026-10-21 实证 stderr 41 次）与 venv 包缺失 ModuleNotFoundError
（同日同步残骸）——都发生在单元测试 mock 掉的这一层，本套件是它们的回归门。

- GBK 最劣构造：spawn 前删净 PYTHONUTF8/PYTHONIOENCODING，zh-CN Windows
  ANSI 代码页下旧实现（无 PYTHONUTF8 钳制）必死于 print_banner 的方块字符
  UnicodeEncodeError；修复后 PYTHONUTF8=1 由 _build_api_env setdefault 注入。
- venv 残缺判活（解释器在、包缺 → 路由进自愈）由 _aux_venv_usable 承载，
  行为测试见 test_hindsight_aux_provision；本套件只在可用 venv 上运行。

无 aux venv 的环境（Linux CI、未供给的开发机）模块级 skip。
"""

from __future__ import annotations

import importlib.util
import json
import sys
import urllib.request
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))


def _load_module() -> object:
    """动态加载 server.py（新建模块名，避免与其他套件的模块级状态串扰）。"""
    mod_name = "hindsight_api_smoke_test"
    path = _PLUGIN_DIR / "server.py"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    assert spec is not None, "Cannot load server.py"
    assert spec.loader is not None, "Cannot load server.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


_mod = _load_module()
if not _mod._aux_venv_usable(str(_PLUGIN_DIR)):
    pytest.skip(
        "aux venv（.venv-hindsight）不可用——真启动冒烟只在已供给的开发机运行"
        "（Linux CI / 未供给环境跳过，缺失判活行为见 test_hindsight_aux_provision）",
        allow_module_level=True,
    )

# 与 8420 生产口/常用调试口错开；被无关进程占用时 _pre_clean_port fail-fast。
_SMOKE_PORT = 18421


def test_api_real_spawn_reaches_health(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """真实 spawn → /health 200：启动契约（编码免疫 + 包在位 + PG 嵌入式初始化）全链。

    spawn 前删净 UTF-8 环境指令 = zh-CN Windows 最劣形态；Embedding 段镜像
    _LOCAL_EMBEDDINGS_ENVS（本地向量模式，与生产 on_load 同源），LLM 段用哑值
    （启动链只验 key 在位，不发起调用——装机 2026-10-21 缺 key 即
    ValueError 退出的启动契约实证）。
    """
    monkeypatch.delenv("PYTHONUTF8", raising=False)
    monkeypatch.delenv("PYTHONIOENCODING", raising=False)
    for env_key, value in _mod._LOCAL_EMBEDDINGS_ENVS.items():
        monkeypatch.setenv(env_key, value)
    monkeypatch.setenv("HINDSIGHT_API_RERANKER_PROVIDER", "rrf")
    monkeypatch.setenv("HINDSIGHT_API_LLM_PROVIDER", "openai")
    monkeypatch.setenv("HINDSIGHT_API_LLM_API_KEY", "sk-smoke-dummy-key")
    monkeypatch.setenv("HINDSIGHT_API_LLM_MODEL", "smoke-dummy-model")

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    proc, stderr_path = _mod._start_api_server(_SMOKE_PORT, str(data_dir))
    try:
        import asyncio

        asyncio.run(
            _mod._wait_api_ready(
                f"http://127.0.0.1:{_SMOKE_PORT}", proc, str(stderr_path)
            )
        )
        with urllib.request.urlopen(
            f"http://127.0.0.1:{_SMOKE_PORT}/health", timeout=5
        ) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        assert body.get("status") == "healthy", f"health 形态异常: {body}"
        assert body.get("database") == "connected", f"嵌入式 PG 未连接: {body}"
    finally:
        _mod.kill_process_tree(proc.pid)
