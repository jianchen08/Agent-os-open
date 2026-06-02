"""真实复盘端到端验证脚本。

本脚本在不依赖 LLM / PipelineEngine 完整启动的前提下，跑通「真实复盘链路」：

1. 真实 ExecutionRecordStorage（YAML 多文件持久化）：
   - 模拟 track 插件写入 ExecutionRecordData（含错误）+ PipelineRunSummary
   - 文件落到临时目录 data/pipelines_real_review_test/<run_id>.yaml

2. 真实 KnowledgeService + JsonMemoryStore（JSON 文件持久化）：
   - create_knowledge 真实写盘到 data/memory_real_review_test/knowledge/*.json
   - list_semantic_memory 真实从磁盘读

3. 真实 ReviewEngine._run_review_full：
   - get_summary / list_by_pipeline / update_summary 都打到真实 storage
   - create_knowledge 真实落 JSON 文件
   - 经验去重逻辑（按 source_type='review_experience' 过滤）真实生效

4. 复盘产出验证：
   - 检查 knowledge 目录下 JSON 文件含 source_type='review_experience'
   - 检查 YAML summary 中 review_status 被翻更为 'completed'
   - 检查第二次跑同样 run_id 时经验去重生效（新增经验数 = 0）

注：ChunkService 接口与 ReviewEngine 当前期望的 save_chunk 不一致（真实为 async save()，
且 ChunkData 字段名不同），因此 _mark_pipeline_reviewed 中的 chunk 标记环节用一个最小
async 桩覆盖，不影响「复盘产出」本身的真实验证。
"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from infrastructure.execution_record_storage import (  # noqa: E402
    ExecutionRecordData,
    ExecutionRecordStorage,
    PipelineRunSummary,
)
from memory.knowledge_service import KnowledgeService  # noqa: E402
from memory.maintenance.review_engine import ChunkData, ReviewEngine  # noqa: E402
from memory.storage.json_store import JsonMemoryStore  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
logger = logging.getLogger("verify_real_review")


# ---------------------------------------------------------------------------
# 最小 async chunk 桩：覆盖 ReviewEngine._mark_pipeline_reviewed 调用
# ---------------------------------------------------------------------------


class _MinimalAsyncChunkDB:
    """最小 async chunk 桩，仅满足 ReviewEngine 当前接口契约。

    ReviewEngine._mark_pipeline_reviewed 会:
      - await self._chunk_db.find_by_pipeline(run_id)
      - self._chunk_db.save_chunk(chunk)   # 注意当前是同步调用

    桩提供两个方法：async find_by_pipeline + 同步 save_chunk，
    后者直接把 reviewed 标记存进内存 extra_data，不影响真实复盘产出验证。
    """

    def __init__(self) -> None:
        """初始化空 chunk 池。"""
        self._chunks: dict[str, list[ChunkData]] = {}

    def seed(self, run_id: str, chunks: list[ChunkData]) -> None:
        """预置一批 chunk 供复盘时读取。"""
        self._chunks[run_id] = list(chunks)

    async def find_by_pipeline(self, run_id: str) -> list[ChunkData]:
        """模拟按 run_id 查找 chunk。"""
        return list(self._chunks.get(run_id, []))

    def save_chunk(self, chunk: ChunkData) -> None:
        """模拟保存 chunk（同步签名匹配 ReviewEngine 现有调用）。"""
        bucket = self._chunks.setdefault(chunk.pipeline_id, [])
        for i, c in enumerate(bucket):
            if c.chunk_id == chunk.chunk_id:
                bucket[i] = chunk
                return
        bucket.append(chunk)


# ---------------------------------------------------------------------------
# 工具：模拟 track 插件向 ExecutionRecordStorage 真实落盘
# ---------------------------------------------------------------------------


def _seed_real_pipeline(
    storage: ExecutionRecordStorage,
    run_id: str,
    error_scenarios: list[tuple[str, str]],
) -> None:
    """模拟 Output/track 插件写入真实的 pipeline 执行记录。

    Args:
        storage: 真实 ExecutionRecordStorage 实例
        run_id: 管道运行 ID
        error_scenarios: [(step_name, error_msg), ...] —— 模拟出错的步骤
    """
    # 1. 写入若干 ExecutionRecordData（与真实 track 插件做的事一致）
    sequence = 0
    for step_name, error_msg in error_scenarios:
        record = ExecutionRecordData(
            record_id=uuid.uuid4().hex[:12],
            pipeline_run_id=run_id,
            type="tool",
            name=step_name,
            sequence=sequence,
            iteration=sequence,
            role="tool",
            content=f"step {step_name} output (will fail)",
            error=error_msg,
        )
        storage.save(record)
        sequence += 1

    # 2. 写入 PipelineRunSummary（status=completed, review_status=pending）
    summary = PipelineRunSummary(
        run_id=run_id,
        thread_id=f"thread-{run_id}",
        total_iterations=len(error_scenarios),
        total_records=len(error_scenarios),
        status="completed",
        final_output="",
        review_status="pending",
    )
    storage.save_summary(summary)
    logger.info(
        "[Seed] 写入真实 YAML 管道记录 | run_id=%s | records=%d | file=%s",
        run_id, len(error_scenarios), storage._data_dir,
    )


# ---------------------------------------------------------------------------
# 主验证流程
# ---------------------------------------------------------------------------


async def _run_real_review_once(
    *,
    pipelines_dir: Path,
    memory_dir: Path,
    tag: str,
) -> dict[str, Any]:
    """跑一次完整的真实复盘流程并返回结果汇总。"""
    # ===== 真实组件实例化 =====
    storage = ExecutionRecordStorage(data_dir=str(pipelines_dir))
    json_store = JsonMemoryStore(data_dir=str(memory_dir))
    knowledge_service = KnowledgeService(semantic_storage=json_store)
    chunk_stub = _MinimalAsyncChunkDB()

    review_engine = ReviewEngine(
        storage=storage,
        chunk_db=chunk_stub,
        knowledge_service=knowledge_service,
        pipeline_engine=None,
    )

    # ===== 模拟 track 插件写入真实管道执行记录 =====
    run_id = f"real-review-{tag}-{uuid.uuid4().hex[:6]}"
    _seed_real_pipeline(
        storage,
        run_id,
        error_scenarios=[
            ("web_search", "API timeout after 30s"),
            ("data_parse", ""),  # 无错的步骤
            ("file_write", "Permission denied: /output/result.txt"),
        ],
    )

    # 预置 1 个 chunk（用于 _mark_pipeline_reviewed 的 chunk 标记环节）
    chunk_stub.seed(
        run_id,
        [
            ChunkData(
                chunk_id=f"chunk-{run_id}",
                pipeline_id=run_id,
                layer="L1",
                content="some compressed chunk content",
                extra_data={"reviewed": False},
            )
        ],
    )

    # ===== 复盘前快照 =====
    summary_before = storage.get_summary(run_id)
    knowledge_files_before = set((memory_dir / "knowledge").glob("*.json"))
    logger.info(
        "[Before %s] review_status=%s | existing_knowledge_files=%d",
        tag, summary_before.review_status, len(knowledge_files_before),
    )

    # ===== 执行真实复盘（走 _run_review_full） =====
    result = await review_engine.run_review(run_id)
    logger.info("[Review %s] result=%s", tag, result)

    # ===== 复盘后快照 =====
    summary_after = storage.get_summary(run_id)
    knowledge_files_after = set((memory_dir / "knowledge").glob("*.json"))
    new_knowledge_files = knowledge_files_after - knowledge_files_before

    # 读取新产出的 knowledge JSON 文件，验证复盘产出真实落盘
    new_experiences: list[dict[str, Any]] = []
    for f in sorted(new_knowledge_files):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            new_experiences.append(data)
        except Exception as e:
            logger.warning("读取 knowledge 文件失败 | file=%s | err=%s", f, e)

    # 验证 chunk 被标记
    chunk_after = chunk_stub._chunks.get(run_id, [])
    chunk_reviewed_flags = {c.chunk_id: c.extra_data.get("reviewed") for c in chunk_after}

    return {
        "tag": tag,
        "run_id": run_id,
        "review_result": result,
        "summary_before": summary_before.review_status,
        "summary_after": summary_after.review_status,
        "new_knowledge_files": [str(f.name) for f in sorted(new_knowledge_files)],
        "new_experiences": new_experiences,
        "chunk_reviewed_flags": chunk_reviewed_flags,
    }


async def _main() -> int:
    """主入口：在独立临时目录跑真实复盘，避免污染项目数据。"""
    tmp_root = Path(tempfile.mkdtemp(prefix="real_review_test_"))
    pipelines_dir = tmp_root / "pipelines"
    memory_dir = tmp_root / "memory"
    pipelines_dir.mkdir(parents=True, exist_ok=True)
    memory_dir.mkdir(parents=True, exist_ok=True)

    logger.info("=== 临时数据目录: %s ===", tmp_root)

    try:
        # ----- 第一轮：对一条新管道跑复盘 -----
        first = await _run_real_review_once(
            pipelines_dir=pipelines_dir, memory_dir=memory_dir, tag="first",
        )

        # ----- 第二轮：再次对同一管道跑复盘，验证经验去重 -----
        # 注意：第二轮用同一 storage/memory，但用一个新的 ReviewEngine 实例
        # （因为第一轮已经把 review_status 翻成 completed，需要直接构造）
        storage = ExecutionRecordStorage(data_dir=str(pipelines_dir))
        json_store = JsonMemoryStore(data_dir=str(memory_dir))
        knowledge_service = KnowledgeService(semantic_storage=json_store)
        chunk_stub = _MinimalAsyncChunkDB()
        review_engine = ReviewEngine(
            storage=storage,
            chunk_db=chunk_stub,
            knowledge_service=knowledge_service,
        )
        # 把 review_status 重置为 pending 以模拟"再次触发"
        storage.update_summary(first["run_id"], {"review_status": "pending"})

        knowledge_before_r2 = set((memory_dir / "knowledge").glob("*.json"))
        result_r2 = await review_engine.run_review(first["run_id"])
        knowledge_after_r2 = set((memory_dir / "knowledge").glob("*.json"))
        new_in_r2 = knowledge_after_r2 - knowledge_before_r2

        # ===================== 验证 =====================
        print("\n" + "=" * 70)
        print("第一轮复盘（首次产出经验）")
        print("=" * 70)
        print(f"run_id              : {first['run_id']}")
        print(f"summary.review_status: {first['summary_before']} -> {first['summary_after']}")
        print(f"review_result       : {first['review_result']}")
        print(f"新产出 knowledge 文件数: {len(first['new_knowledge_files'])}")
        for f in first["new_knowledge_files"]:
            print(f"  - {f}")
        print(f"新经验条目数        : {len(first['new_experiences'])}")
        for exp in first["new_experiences"]:
            print(
                f"  - id={exp.get('id')}  source_type={exp.get('source_type')}  "
                f"user_id={exp.get('user_id')}"
            )
            print(f"    content: {exp.get('content')}")
        print(f"chunk reviewed 标记 : {first['chunk_reviewed_flags']}")

        print("\n" + "=" * 70)
        print("第二轮复盘（验证经验去重）")
        print("=" * 70)
        print(f"review_result       : {result_r2}")
        print(f"新增 knowledge 文件 : {len(new_in_r2)} （应为 0，已存在的经验应被去重）")

        # ===== 断言 =====
        failures: list[str] = []
        if first["summary_after"] != "completed":
            failures.append(
                f"summary.review_status 应为 completed，实际 {first['summary_after']}"
            )
        if first["review_result"].get("status") != "success":
            failures.append(f"第一轮 result.status 应为 success")
        if first["review_result"].get("experience_count") != 2:
            failures.append(
                f"第一轮应产出 2 条经验（2 个错误步骤），"
                f"实际 experience_count={first['review_result'].get('experience_count')}"
            )
        if len(first["new_experiences"]) != 2:
            failures.append(
                f"应新写出 2 个 knowledge JSON 文件，实际 {len(first['new_experiences'])}"
            )
        for exp in first["new_experiences"]:
            if exp.get("source_type") != "review_experience":
                failures.append(
                    f"经验 source_type 应为 review_experience，实际 {exp.get('source_type')}"
                )
            if exp.get("user_id") != "system":
                failures.append(
                    f"经验 user_id 应为 system，实际 {exp.get('user_id')}"
                )
        if not all(first["chunk_reviewed_flags"].values()):
            failures.append(
                f"chunk.extra_data.reviewed 应全部为 True，实际 {first['chunk_reviewed_flags']}"
            )
        if result_r2.get("experience_count") != 0:
            failures.append(
                f"第二轮去重后 experience_count 应为 0，实际 {result_r2.get('experience_count')}"
            )
        if len(new_in_r2) != 0:
            failures.append(
                f"第二轮不应新增 knowledge 文件，实际新增 {len(new_in_r2)}"
            )

        print("\n" + "=" * 70)
        if failures:
            print("❌ 真实复盘验证失败：")
            for f in failures:
                print(f"  - {f}")
            return 1
        print("✅ 真实复盘端到端验证通过：")
        print("  1. ExecutionRecordStorage 真实 YAML 落盘成功")
        print("  2. ReviewEngine._run_review_full 真实流程跑通")
        print("  3. KnowledgeService.create_knowledge 真实写 JSON 文件")
        print("  4. YAML summary 的 review_status 被翻更为 completed")
        print("  5. chunk.extra_data.reviewed 被翻更为 True")
        print("  6. 经验去重（source_type=review_experience）真实生效")
        print("=" * 70)
        return 0
    finally:
        # 保留临时目录便于人工 inspect；如需自动清理可取消注释
        # shutil.rmtree(tmp_root, ignore_errors=True)
        logger.info("保留临时目录以便 inspect: %s", tmp_root)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
