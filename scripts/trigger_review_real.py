"""复盘触发脚本（真实数据版）- 基于 data/pipelines/ 下的管道记录触发复盘。

用法: python3 scripts/trigger_review_real.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import yaml

# 确保项目根目录在 sys.path 中
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.memory.maintenance.review_engine import (
    ChunkData,
    ExecutionRecord,
    PipelineRunSummary,
    ReviewEngine,
)


# ---------------------------------------------------------------------------
# 轻量级存储适配器
# ---------------------------------------------------------------------------

class _YamlStorage:
    """从 data/pipelines/ 读取 YAML 文件的轻量存储实现。"""

    def __init__(self, data_dir: Path) -> None:
        self._summaries: dict[str, PipelineRunSummary] = {}
        self._records: dict[str, list[ExecutionRecord]] = {}
        self._load_all(data_dir)

    def _load_all(self, data_dir: Path) -> None:
        for yaml_file in sorted(data_dir.glob("*.yaml")):
            try:
                text = yaml_file.read_text(encoding="utf-8")
                data = yaml.safe_load(text)
            except Exception as exc:
                print(f"  ⚠ 跳过损坏文件 {yaml_file.name}: {exc}")
                continue

            if not isinstance(data, dict):
                continue

            # 加载 summary
            summary_dict = data.get("summary")
            if summary_dict and isinstance(summary_dict, dict):
                summary = PipelineRunSummary(
                    run_id=summary_dict.get("run_id", ""),
                    total_records=summary_dict.get("total_records", 0),
                    total_iterations=summary_dict.get("total_iterations", 0),
                    created_at=summary_dict.get("created_at", ""),
                    status=summary_dict.get("status", ""),
                    error=summary_dict.get("error") or "",
                    review_status=summary_dict.get("review_status", "pending"),
                )
                self._summaries[summary.run_id] = summary

            # 加载 records
            run_id = summary_dict.get("run_id", "") if summary_dict else ""
            records_list = data.get("records") or []
            parsed: list[ExecutionRecord] = []
            for r in records_list:
                if not isinstance(r, dict):
                    continue
                parsed.append(ExecutionRecord(
                    iteration=r.get("iteration", 0),
                    type=r.get("type", ""),
                    name=r.get("name", ""),
                    error=r.get("error") or "",
                    thinking_content=r.get("thinking_content"),
                    tool_calls_json=r.get("tool_calls_json"),
                    content=r.get("content", ""),
                    sequence=r.get("sequence", 0),
                ))
            self._records[run_id] = parsed

    def get_summary(self, run_id: str) -> PipelineRunSummary | None:
        return self._summaries.get(run_id)

    def list_by_pipeline(self, run_id: str) -> list[ExecutionRecord]:
        return self._records.get(run_id, [])

    def list_all_summaries(self) -> list[PipelineRunSummary]:
        return list(self._summaries.values())

    def update_summary(self, run_id: str, data: dict[str, str]) -> None:
        summary = self._summaries.get(run_id)
        if summary:
            for key, value in data.items():
                setattr(summary, key, value)


class _InMemoryChunkDB:
    """内存 chunk 存储。"""

    def __init__(self) -> None:
        self._chunks: dict[str, list[ChunkData]] = {}
        self._saved: list[ChunkData] = []

    def add_chunks(self, run_id: str, chunks: list[ChunkData]) -> None:
        self._chunks[run_id] = chunks

    async def find_by_pipeline(self, run_id: str) -> list[ChunkData]:
        return self._chunks.get(run_id, [])

    def save_chunk(self, chunk: ChunkData) -> None:
        """保存 chunk（供 ReviewEngine 调用）。"""
        self._saved.append(chunk)


class _InMemoryKnowledgeService:
    """内存知识服务。"""

    def __init__(self) -> None:
        self._items: list[dict[str, str]] = []

    async def list_semantic_memory(self, user_id: str = "") -> dict[str, Any]:
        return {"items": self._items, "total": len(self._items)}

    async def create_knowledge(self, **kwargs: Any) -> dict[str, str]:
        self._items.append({
            "content": kwargs.get("content", ""),
            "source_type": kwargs.get("source_type", ""),
        })
        return {"id": f"k-{len(self._items)}", "status": "created"}


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

async def _run() -> int:
    data_dir = Path(__file__).resolve().parent.parent / "data" / "pipelines"
    if not data_dir.exists():
        print(f"错误: 数据目录不存在 {data_dir}")
        return 1

    print("=" * 60)
    print("复盘触发脚本（真实数据版）启动")
    print("=" * 60)

    # 构建轻量依赖
    storage = _YamlStorage(data_dir)
    chunk_db = _InMemoryChunkDB()
    ks = _InMemoryKnowledgeService()

    # 为每个 pipeline 预创建 chunk
    for summary in storage.list_all_summaries():
        chunk_db.add_chunks(summary.run_id, [
            ChunkData(
                chunk_id=f"chunk-{summary.run_id}",
                pipeline_id=summary.run_id,
                layer="summary",
                content=f"Pipeline {summary.run_id} 执行摘要",
            )
        ])

    engine = ReviewEngine(
        storage=storage,
        chunk_db=chunk_db,
        knowledge_service=ks,
    )

    # 获取待复盘列表
    pending = engine.get_pending_pipelines()
    print(f"\n数据目录: {data_dir}")
    print(f"已加载管道总数: {len(storage.list_all_summaries())}")
    print(f"待复盘管道数量: {len(pending)}")

    if not pending:
        print("\n没有待复盘的管道。")
        return 0

    print("\n" + "-" * 40)
    print("开始逐个复盘:")
    print("-" * 40)

    total_experiences = 0
    for summary in pending:
        print(f"\n📋 复盘管道: {summary.run_id}")
        print(f"   状态: {summary.status} | 记录数: {summary.total_records} | 迭代数: {summary.total_iterations}")

        result = await engine.run_review(summary.run_id)

        if result["status"] == "success":
            print(f"   ✅ 复盘成功")
            print(f"   分析记录数: {result['records_analyzed']}")
            print(f"   提取经验数: {result['experience_count']}")
            total_experiences += result["experience_count"]

            # 打印提取的经验
            for item in ks._items:
                if summary.run_id in item.get("content", ""):
                    print(f"   💡 {item['content']}")
        else:
            print(f"   ❌ 复盘失败: {result.get('message', '未知错误')}")

    # 总结
    print("\n" + "=" * 60)
    print(f"复盘完成 ✓ 共处理 {len(pending)} 个管道，提取 {total_experiences} 条经验")
    print("=" * 60)

    # 验证状态
    reviewed_count = sum(
        1 for s in storage.list_all_summaries()
        if s.review_status == "completed"
    )
    print(f"已复盘: {reviewed_count} | 待复盘: {len(pending) - reviewed_count}")

    return 0


def main() -> int:
    return asyncio.run(_run())


if __name__ == "__main__":
    sys.exit(main())
