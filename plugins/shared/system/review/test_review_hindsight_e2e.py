# @feature: FP-0.2.六 记忆检索 | @vision: V1 可进化 | @ci: none-local
"""review 报告持久化 → hindsight 真实后端 e2e round-trip（缺陷①②修复判据）。

真实链路（不走内核，MCP transport 由 tool-executor 直调 sidecar 工具替代）：

    store_report（review server）
      → HindsightBackend.add（wire metadata 键值全 str + review_id 定向键）
      → hindsight sidecar retain handler（tags 提升）
      → 真实 hindsight-api 0.9.1（.venv-hindsight 子进程，auto-spawn 同款）
    清空 _reports（模拟 sidecar 重启丢内存）
    get_report → _cold_read_report
      → HindsightBackend.get_documents（tags=review_id:<id> any_strict）
      → hindsight sidecar get_documents handler（documents API 取原文）
      → original_text JSON 解析 → 完整报告

通过判据：取回报告 review_id/status/lessons 与写入一致（真 e2e 闭环）。
清理：adelete_bank（同时回归 hindsight_delete 修复的真实路径）。

环境门槛（缺失即 skip，CI 无 .venv-hindsight 时不红）：
- .venv-hindsight 解释器存在（hindsight-api 服务器栈）
- 项目根 .env 有 ZHIPU_API_KEY / SILICONFLOW_API_KEY（LLM 抽取 + embedding）

[来源: docs/working/插件uv运行时迁移方案_20260819.md §12.4① 修复]
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any

import pytest

_PLUGIN_DIR = Path(__file__).resolve().parent
_HINDSIGHT_DIR = _PLUGIN_DIR.parent / "hindsight_memory"
_PROJECT_ROOT = _PLUGIN_DIR.parents[3]

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
    """起真实 hindsight-api（临时工作目录隔离 pg0 数据）+ 双插件模块栈。

    Yields:
        (hmod, rmod)——hindsight sidecar 模块（_client 已接真实 API）与
        review 模块（_memory_backend 已注入直连 sidecar 工具的 HindsightBackend）。
    """
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

        # tool-executor 替身：直调 sidecar 工具 handler（真实 retain/get_documents
        # 链路，仅省去内核 MCP transport——该段批 C 已由 /ext 生产数据证明）
        async def caller(method: str, params: dict[str, Any]) -> Any:
            assert method == "tool-executor.invoke", method
            tool = hmod.plugin._tools[params["tool_name"]]
            result = tool.handler(**params["args"])
            if asyncio.iscoroutine(result):
                result = await result
            return result

        from memory_backend import HindsightBackend  # noqa: PLC0415

        rmod = _load_module(_PLUGIN_DIR / "server.py", "review_server_e2e")
        rmod._reports.clear()
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
    """轮询等 retain_async 异op 落地（LLM 抽取管线 ~5-15s）后文档可列。"""
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        docs = await hmod.hindsight_get_documents(
            bank_id=bank, tags=tags, tags_match="any_strict", limit=5
        )
        if docs.get("documents"):
            return docs["documents"]
        await asyncio.sleep(2)
    return []


class TestReviewHindsightRoundTrip:
    async def test_store_report_survives_restart_cold_read(
        self, hindsight_stack: Any
    ) -> None:
        """真 e2e 闭环：store_report → 清空 _reports（模拟重启）→ get_report
        取回完整报告（原文 original_text，非抽取事实）。"""
        hmod, rmod = hindsight_stack
        review_id = "review_e2e_roundtrip_1"
        bank = "review"  # store_report 固定写 "review" bank（冷读定向前提）
        report = {
            "task_id": "task-e2e-1",
            "summary": "e2e 冷读回归复盘",
            "lessons": ["lesson-e2e-1", "lesson-e2e-2"],
            "recommendations": ["rec-e2e-1"],
        }

        # ① 写入（缺陷①修复面：metadata 键值全 str + review_id 定向键 + tags 提升）
        await rmod.store_report(review_id, report)
        assert rmod._reports[review_id]["status"] == "completed"

        # ② 等 retain_async 异步管线落地（文档可见 = 写入真正持久化）
        docs = await _wait_document_visible(
            hmod, bank, [f"review_id:{review_id}"]
        )
        assert docs, "retain 后文档从未可见——写入未真正持久化（缺陷①回归）"
        doc_meta = docs[0].get("document_metadata") or {}
        assert doc_meta.get("review_id") == review_id, doc_meta
        assert "review_id:" + review_id in (docs[0].get("tags") or [])

        # ③ 模拟重启：清空内存态
        rmod._reports.clear()
        assert review_id not in rmod._reports

        # ④ 冷读取回完整报告（缺陷②修复面：documents 原文，非 recall 抽取事实）
        got = await rmod.get_report(review_id)
        assert got.get("error") is None, got
        assert got["review_id"] == review_id
        assert got["status"] == "completed"
        assert got["task_id"] == "task-e2e-1"
        assert got["lessons"] == ["lesson-e2e-1", "lesson-e2e-2"]
        assert got["recommendations"] == ["rec-e2e-1"]
        # 回填内存
        assert rmod._reports[review_id]["status"] == "completed"

        # ⑤ 清理：删 bank（真实 adelete_bank 路径 = hindsight_delete 修复回归）
        client = hmod._client
        await client.adelete_bank(bank_id=bank)
        remaining = await hmod.hindsight_get_documents(
            bank_id=bank, tags=[f"review_id:{review_id}"], limit=5
        )
        assert not remaining.get("documents"), "bank 删除后仍有残留文档"
