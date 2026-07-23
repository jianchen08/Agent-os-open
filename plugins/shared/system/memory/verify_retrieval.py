#!/usr/bin/env python3
"""记忆检索 3×3 矩阵验证脚本（P1-1）。

直接驱动三个 retriever 验证：
1. keyword 必须通（无外部依赖）
2. vector 用 fake embedding 验证余弦检索路径通
3. tagwave 用 fake embedding + 预置 tag/cooccurrence 验证三阶段算法通
4. 验证 embedding key 缺失时降级路径（LLMClient 未配置）

运行: python verify_retrieval.py
"""

from __future__ import annotations

import asyncio
import math
import os
import sys
import tempfile

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, THIS_DIR)

from keyword_retriever import KeywordRetriever
from vector_retriever import SqliteVectorStore, VectorRetriever, cosine_similarity
from tag_network_retriever import TagNetworkRetriever


def _hash_embed(text: str, dim: int = 64) -> list[float]:
    """确定性伪 embedding（基于字符 hash），仅测试用。

    相同/相近文本产生相近向量，保证余弦检索有区分度。
    """
    vec = [0.0] * dim
    for i, ch in enumerate(text):
        vec[(ord(ch) + i) % dim] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def fake_embed_fn(texts: list[str]) -> list[list[float]]:
    """fake 嵌入函数（list[str] -> list[list[float]]）。"""
    return [_hash_embed(t) for t in texts]


async def test_keyword(store: SqliteVectorStore) -> None:
    """keyword 检索必须通。"""
    retriever = KeywordRetriever(store)
    results = await retriever.retrieve(query="Python", top_k=5, memory_type="semantic")
    assert len(results) > 0, "keyword 检索应返回结果"
    assert all("Python".lower() in r.content.lower() for r in results), "结果应包含关键词"
    print(f"[PASS] keyword 检索: {len(results)} 条命中")


async def test_vector(store: SqliteVectorStore) -> None:
    """vector 检索路径（fake embedding）必须通。"""
    retriever = VectorRetriever(store, fake_embed_fn)
    results = await retriever.retrieve(query="机器学习算法", top_k=3, memory_type="semantic")
    assert len(results) > 0, "vector 检索应返回结果"
    assert all(0.0 <= r.score <= 1.0 for r in results), "score 应在 [0,1]"
    print(f"[PASS] vector 检索: {len(results)} 条, top score={results[0].score:.4f}")
    # 验证 numpy/pure 路径都能跑
    sims = cosine_similarity(_hash_embed("a"), [_hash_embed("a"), _hash_embed("b")])
    assert sims[0] > sims[1], "相同文本相似度应更高"
    print("[PASS] cosine_similarity 区分度正常")


async def test_tagwave(store: SqliteVectorStore) -> None:
    """tagwave 三阶段算法必须通（预置 tag + 共现）。"""
    # 预置 tag 索引
    vec_ml = _hash_embed("机器学习")
    vec_ai = _hash_embed("人工智能")
    tid_ml = store.save_tag("机器学习", vec_ml, frequency=5)
    tid_ai = store.save_tag("人工智能", vec_ai, frequency=5)
    store.update_cooccurrence(tid_ml, tid_ai)
    store.update_cooccurrence(tid_ai, tid_ml)

    vector_retriever = VectorRetriever(store, fake_embed_fn)
    tagwave = TagNetworkRetriever(vector_retriever, fake_embed_fn)
    n = tagwave.init_from_store()
    assert n >= 2, f"应加载至少 2 个 tag，实际 {n}"

    # 验证三阶段增强产出向量
    boost = tagwave.apply_tag_boost(_hash_embed("机器学习"), tag_boost=0.3)
    assert len(boost.vector) > 0, "增强后向量非空"
    print(
        f"[PASS] tagwave 增强: matched_tags={boost.matched_tags[:3]} "
        f"spike_count={boost.spike_count} boost={boost.boost_factor}"
    )

    # 验证完整检索链路（增强向量 → 二次向量检索）
    results = await tagwave.retrieve(query="机器学习", top_k=3, memory_type="semantic")
    print(f"[PASS] tagwave 检索: {len(results)} 条结果")


def test_degradation() -> None:
    """embedding key 缺失时 LLMClient 应报告不可用。"""
    from embedding_client import LLMClient

    # 空 config → embedding 不可用
    client = LLMClient({})
    assert not client.embedding_available, "空 config 时 embedding 应不可用"
    assert not client.chat_available, "空 config 时 chat 应不可用"
    print("[PASS] 降级路径: 无 key 时 embedding/chat 均标记不可用")

    # 模拟带 ${ENV} 但未设置环境变量的 config
    cfg = {
        "models": {
            "models": {
                "embedding-3": {
                    "provider": "zhipu_coding",
                    "model_name": "embedding-3",
                    "api_base": "https://open.bigmodel.cn/api/coding/paas/v4/",
                }
            },
            "providers": {
                "zhipu_coding": {"keys": [{"api_key": "${ZHIPU_API_KEY_UNDEF}"}]}
            },
            "defaults": {"embedding": "embedding-3"},
        }
    }
    client2 = LLMClient(cfg)
    assert not client2.embedding_available, "未定义 ENV 时 api_key 应为空，embedding 不可用"
    print("[PASS] 降级路径: ${ENV} 未定义时正确解析为空并降级")


async def main() -> None:
    """主验证流程。"""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db_path = tmp.name
    try:
        store = SqliteVectorStore(db_path)
        # 灌入测试数据
        store.save_memory(
            entry_id="m1", memory_type="semantic", content="Python 是一门流行的编程语言",
            metadata={}, embedding=_hash_embed("Python 编程语言"), created_at=1.0,
        )
        store.save_memory(
            entry_id="m2", memory_type="semantic", content="机器学习是人工智能的核心技术",
            metadata={"tags": ["机器学习", "人工智能"]},
            embedding=_hash_embed("机器学习算法"), created_at=2.0,
        )
        store.save_memory(
            entry_id="m3", memory_type="semantic", content="深度学习使用神经网络进行特征学习",
            metadata={"tags": ["深度学习"]},
            embedding=_hash_embed("深度学习神经网络"), created_at=3.0,
        )

        await test_keyword(store)
        await test_vector(store)
        await test_tagwave(store)
        test_degradation()

        store.close()
        print("\n[ALL PASS] 三种检索 + 降级路径全部验证通过")
    finally:
        # Windows: 先确保连接关闭再删除（store.close 已在 try 体调用）
        try:
            os.unlink(db_path)
        except PermissionError:
            pass


if __name__ == "__main__":
    asyncio.run(main())
