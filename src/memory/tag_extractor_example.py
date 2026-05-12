"""
TagExtractor 使用示例

展示如何使用 TagExtractor 类从 L3 压缩内容和知识库内容中提取标签
"""

import asyncio

from src.core.embeddings import EmbeddingService
from src.memory.tag_extractor import TagExtractor


# ============ 示例 1: 基础用法（仅使用规则解析） ============
async def example_basic_usage():
    """
    基础用法示例 - 不使用 LLM，仅使用规则解析

    适用于：
    - 简单的关键词提取
    - 性能敏感的场景
    - 无需 LLM 调用的离线处理
    """
    print("=" * 60)
    print("示例 1: 基础用法（仅使用规则解析）")
    print("=" * 60)

    # 创建提取器（不传入 LLM 客户端）
    extractor = TagExtractor()

    # 示例 L3 内容（包含 "关键词：" 格式）
    l3_content = """
关键词：Python, 异步编程, FastAPI, Web框架, 高性能
核心概念：现代Python Web开发框架，支持异步请求处理
"""

    # 从 L3 提取关键词
    tags = await extractor.extract_from_l3(l3_content)
    print(f"L3 内容: {l3_content.strip()}")
    print(f"提取的标签: {tags}")
    print()

    # 示例知识库内容
    knowledge_content = """
    FastAPI 是一个现代、快速（高性能）的 Web 框架，用于基于标准 Python 类型提示构建 API。
    它的主要特性包括：快速、高效、易于学习和使用。
    FastAPI 基于 Starlette 和 Pydantic，是 Python 最快的 Web 框架之一。
    """

    # 从知识库提取标签（降级为词频提取）
    tags = await extractor.extract_from_knowledge(knowledge_content)
    print(f"知识内容: {knowledge_content.strip()[:100]}...")
    print(f"提取的标签（词频）: {tags}")
    print()


# ============ 示例 2: 使用 LLM 进行智能提取 ============
async def example_with_llm():
    """
    使用 LLM 进行智能标签提取

    适用于：
    - 需要语义理解的场景
    - 提取主题标签
    - 内容格式不固定的情况
    """
    print("=" * 60)
    print("示例 2: 使用 LLM 进行智能提取")
    print("=" * 60)

    # 假设我们有一个 LLM 客户端
    # 实际使用时需要传入真实的 LLMClient 实例
    # llm_client = get_llm_client()  # 从工厂获取

    # 这里使用 None 演示接口，实际使用时需要传入真实的客户端
    llm_client = None  # 替换为实际的 LLMClient 实例

    # 创建提取器
    extractor = TagExtractor(llm_client=llm_client)

    # L3 内容（不包含标准格式，需要 LLM 提取）
    l3_content = """
    本次对话主要讨论了 Python 的异步编程模型，特别是 asyncio 库的使用。
    用户希望了解如何在实际项目中应用异步编程来提高性能。
    我们探讨了事件循环、协程、任务等核心概念。
    """

    # 如果没有 LLM 客户端，会使用降级方案
    tags = await extractor.extract_from_l3(l3_content)
    print(f"L3 内容: {l3_content.strip()[:100]}...")
    print(f"提取的关键词: {tags}")
    print()

    # 知识库内容
    knowledge_content = """
    PostgreSQL 是一个强大的开源对象关系数据库系统。
    它拥有超过 35 年的活跃开发历史，以其可靠性、功能稳健性和性能而闻名。
    PostgreSQL 支持复杂的查询、外键、触发器、视图和事务完整性。
    """

    tags = await extractor.extract_from_knowledge(knowledge_content)
    print(f"知识内容: {knowledge_content.strip()[:100]}...")
    print(f"提取的主题标签: {tags}")
    print()


# ============ 示例 3: 使用 Embedding 进行语义匹配 ============
async def example_with_embedding():
    """
    使用 Embedding 进行语义标签匹配

    适用于：
    - 有预定义标签库的场景
    - 需要按语义相似度排序的场景
    - 标签分类任务
    """
    print("=" * 60)
    print("示例 3: 使用 Embedding 进行语义匹配")
    print("=" * 60)

    # 创建嵌入服务
    embedding_service = EmbeddingService()

    # 创建提取器
    extractor = TagExtractor(embedding_service=embedding_service)

    # 输入内容
    content = "如何使用 Python 进行数据分析？"

    # 候选标签库
    candidate_tags = [
        "Python",
        "数据分析",
        "机器学习",
        "Web开发",
        "数据库",
        "人工智能",
        "数据可视化",
        "Pandas",
        "NumPy",
        "深度学习",
    ]

    try:
        # 提取最相关的标签
        related_tags = await extractor.extract_with_embedding(
            content=content,
            candidate_tags=candidate_tags,
            top_k=3,
        )
        print(f"输入内容: {content}")
        print(f"候选标签: {candidate_tags}")
        print(f"最相关的 3 个标签: {related_tags}")
    except Exception as e:
        print(f"Embedding 提取失败: {e}")
    print()


# ============ 示例 4: 完整功能演示 ============
async def example_full_featured():
    """
    完整功能演示 - 结合 LLM 和 Embedding

    适用于：
    - 生产环境
    - 需要高质量标签提取的场景
    """
    print("=" * 60)
    print("示例 4: 完整功能演示")
    print("=" * 60)

    # 初始化服务（实际使用时需要传入真实实例）
    # llm_client = get_llm_client()
    # embedding_service = EmbeddingService()

    llm_client = None
    embedding_service = None

    # 创建完整配置的提取器
    extractor = TagExtractor(
        llm_client=llm_client,
        embedding_service=embedding_service,
    )

    # 查看提取器状态
    stats = extractor.get_stats()
    print(f"提取器状态: {stats}")

    # 处理 L3 压缩内容
    l3_content = "关键词：机器学习, 神经网络, TensorFlow, 深度学习模型训练"
    l3_tags = await extractor.extract_from_l3(l3_content)
    print(f"\nL3 内容: {l3_content}")
    print(f"提取结果: {l3_tags}")

    # 处理知识库内容
    knowledge = """
    Docker 是一个开源的应用容器引擎，让开发者可以打包他们的应用以及依赖包到一个可移植的容器中，
    然后发布到任何流行的 Linux 机器上，也可以实现虚拟化。
    容器是完全使用沙箱机制，相互之间不会有任何接口。
    """
    knowledge_tags = await extractor.extract_from_knowledge(knowledge)
    print(f"\n知识内容: {knowledge.strip()[:80]}...")
    print(f"提取结果: {knowledge_tags}")


# ============ 示例 5: 批量处理 ============
async def example_batch_processing():
    """
    批量处理示例

    适用于：
    - 需要处理大量内容的场景
    - 数据导入/迁移任务
    """
    print("=" * 60)
    print("示例 5: 批量处理")
    print("=" * 60)

    extractor = TagExtractor()

    # 批量 L3 内容
    l3_contents = [
        "关键词：Python, Django, Web开发, ORM",
        "关键词：React, 前端, JavaScript, 组件化",
        "关键词：Kubernetes, 容器编排, 微服务, DevOps",
    ]

    print("批量处理 L3 内容:")
    for i, content in enumerate(l3_contents, 1):
        tags = await extractor.extract_from_l3(content)
        print(f"  {i}. {content}")
        print(f"     -> {tags}")

    # 批量知识库内容
    knowledges = [
        "Redis 是一个开源的内存数据结构存储系统，可以用作数据库、缓存和消息代理。",
        "MongoDB 是一个基于分布式文件存储的数据库，使用 JSON 格式存储数据。",
        "Elasticsearch 是一个分布式、RESTful 风格的搜索和数据分析引擎。",
    ]

    print("\n批量处理知识内容:")
    for i, content in enumerate(knowledges, 1):
        tags = await extractor.extract_from_knowledge(content)
        print(f"  {i}. {content[:50]}...")
        print(f"     -> {tags}")


# ============ 主函数 ============
async def main():
    """运行所有示例"""
    print("\n" + "=" * 60)
    print("TagExtractor 使用示例")
    print("=" * 60 + "\n")

    # 运行示例
    await example_basic_usage()
    await example_with_llm()
    await example_with_embedding()
    await example_full_featured()
    await example_batch_processing()

    print("\n" + "=" * 60)
    print("所有示例运行完成")
    print("=" * 60)


if __name__ == "__main__":
    # 运行异步主函数
    asyncio.run(main())
