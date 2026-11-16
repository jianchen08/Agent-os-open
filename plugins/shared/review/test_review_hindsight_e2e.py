# @feature: FP-0.2.六 记忆检索 | @vision: V1 可进化 | @ci: none-local
"""review 抽取事实 → hindsight 真实后端 e2e（F9 收敛后的记忆面契约）。

F9 收敛后 review 的 Hindsight 职责只剩「面向召回的抽取事实」——报告本体走
文件面、状态生命周期走 state 索引，均不经记忆库。本文件验证的 wire 链路：

    store_report（review server，抽取事实形态）
      → HindsightBackend.add（wire metadata 键值全 str + review_id 定向键）
      → hindsight sidecar retain handler（tags 提升）
      → 真实 hindsight-api 0.9.1（.venv-hindsight 子进程，auto-spawn 同款）
    轮询等 retain_async 落地 → documents 可列（tag 定向命中）

通过判据：文档可见、tag/metadata 定向键齐备、content 为事实文本而非报告
全文 JSON。清理：adelete_bank（同时回归 hindsight_delete 修复的真实路径）。

环境门槛（缺失即 skip，CI 无 .venv-hindsight 时不红）：
- .venv-hindsight 解释器存在（hindsight-api 服务器栈）
- 项目根 .env 有 ZHIPU_API_KEY / SILICONFLOW_API_KEY（LLM 抽取 + embedding）

[来源: docs/working/state单一真相源审查_20261008.md F9 用户裁定精化]
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any

import pytest

_PLUGIN_DIR = Path(__file__).resolve().parent
_HINDSIGHT_DIR = _PLUGIN_DIR.parent / "hindsight_memory"
_PROJECT_ROOT = _PLUGIN_DIR.parents[2]

for _p in (_PLUGIN_DIR, _HINDSIGHT_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


def _venv_python() -> str:
    win = _HINDSIGHT_DIR / ".venv-hindsight" / "Scripts" / "python.exe"
    if win.is_file():
        return str(win)
    unix = _HINDSIGHT_DIR / ".venv-hindsight" / "bin" / "python"
    if unix.is_file():
        return str(unix)
    return ""


def _env_keys() -> dict[str, str]:
    out: dict[str, str] = {}
    env_path = _PROJECT_ROOT / ".env"
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k in ("ZHIPU_API_KEY", "SILICONFLOW_API_KEY"):
                out[k] = v
    except OSError:
        pass
    return out


_VENV = _venv_python()
_KEYS = _env_keys()
pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        not _VENV, reason="hindsight API venv (.venv-hindsight) 不存在——CI 树跳过"
    ),
    pytest.mark.skipif(
        not (_KEYS.get("ZHIPU_API_KEY") and _KEYS.get("SILICONFLOW_API_KEY")),
        reason=".env 缺 ZHIPU_API_KEY/SILICONFLOW_API_KEY（LLM 抽取+embedding）",
    ),
]


def _load_module(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None, f"cannot load {path}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_E2E_PORT = 8422  # 独立端口（8420 为生产、probe 用过 8421）


@pytest.fixture(scope="module")
async def hindsight_stack(tmp_path_factory: Any) -> Any:
    """起真实 hindsight-api（临时工作目录隔离 pg0 数据）+ 双插件模块栈。"""
    hmod = _load_module(_HINDSIGHT_DIR / "server.py", "hindsight_server_e2e")
    hmod._apply_llm_env()  # 与 on_load 同款 env 装配（GLM + bge-m3 + rrf）
    workdir = tmp_path_factory.mktemp("hindsight_e2e_")

    proc = subprocess.Popen(
        [_VENV, "-m", "hindsight_api.main",
         "--port", str(_E2E_PORT), "--host", "127.0.0.1"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=os.environ.copy(),
        cwd=str(workdir),
    )
    base_url = f"http://127.0.0.1:{_E2E_PORT}"
    try:
        ready = False
        for _ in range(60):
            await asyncio.sleep(1)
            try:
                with urllib.request.urlopen(f"{base_url}/health", timeout=2) as resp:
                    if resp.status == 200:
                        ready = True
                        break
            except Exception:
                if proc.poll() is not None:
                    raise RuntimeError(
                        f"hindsight-api 启动即退出 code={proc.returncode}"
                    )
        assert ready, "hindsight-api 60s 未就绪"

        from hindsight_client import Hindsight  # sidecar venv 内 0.9.1

        client = Hindsight(base_url=base_url)
        hmod._client = client  # 与 on_load 等价：模块级 client 注入

        # tool-executor 替身：直调 sidecar 工具 handler（真实 retain 链路，
        # 仅省去内核 MCP transport）
        async def caller(method: str, params: dict[str, Any]) -> Any:
            assert method == "tool-executor.invoke", method
            tool = hmod.plugin._tools[params["tool_name"]]
            result = tool.handler(**params["args"])
            if asyncio.iscoroutine(result):
                result = await result
            return result

        from memory_backend import HindsightBackend  # noqa: PLC0415

        rmod = _load_module(_PLUGIN_DIR / "server.py", "review_server_e2e")
        rmod.set_memory_backend(HindsightBackend(caller))

        yield hmod, rmod

        await client.aclose()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()


async def _wait_document_visible(
    hmod: Any, bank: str, tags: list[str], timeout_s: float = 60.0
) -> list[dict[str, Any]]:
    """轮询等 retain_async 异步管线（LLM 抽取 ~5-15s）后文档可列。"""
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        docs = await hmod.hindsight_get_documents(
            bank_id=bank, tags=tags, tags_match="any_strict", limit=5
        )
        if docs.get("documents"):
            return docs["documents"]
        await asyncio.sleep(2)
    return []


class TestReviewRecallFactE2E:
    async def test_store_report_lands_recall_fact_document(
        self, hindsight_stack: Any
    ) -> None:
        """真 e2e：store_report（抽取事实）→ 文档可见 + 定向键齐备 + 非全文 JSON。"""
        hmod, rmod = hindsight_stack
        review_id = "review_e2e_fact_1"
        bank = "review"  # 固定 "review" bank（定向召回前提）

        # ① 写入抽取事实（wire 契约：metadata 键值全 str + review_id 定向键）
        await rmod.store_report(
            review_id,
            {
                "task_id": "task-e2e-1",
                "summary": "e2e 抽取事实回归复盘",
                "lessons": ["lesson-e2e-1"],
                "recommendations": ["rec-e2e-1"],
            },
        )

        # ② 等 retain_async 落地（文档可见 = 写入真正持久化）
        docs = await _wait_document_visible(hmod, bank, [f"review_id:{review_id}"])
        assert docs, "retain 后文档从未可见——写入未真正持久化（wire 契约回归）"
        doc_meta = docs[0].get("document_metadata") or {}
        assert doc_meta.get("review_id") == review_id, doc_meta
        assert "review_id:" + review_id in (docs[0].get("tags") or [])

        # ③ content 是事实文本线索，不是报告全文 JSON
        content = docs[0].get("original_text") or ""
        assert review_id in content
        with pytest.raises(json.JSONDecodeError):
            json.loads(content)

        # ④ 清理：删 bank（真实 adelete_bank 路径 = hindsight_delete 修复回归）
        client = hmod._client
        await client.adelete_bank(bank_id=bank)
        remaining = await hmod.hindsight_get_documents(
            bank_id=bank, tags=[f"review_id:{review_id}"], limit=5
        )
        assert not remaining.get("documents"), "bank 删除后仍有残留文档"
