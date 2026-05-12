"""
嵌入服务

提供文本嵌入和语义搜索能力
"""


class EmbeddingService:
    """
    嵌入服务

    负责生成文本向量嵌入
    """

    def __init__(self, model_alias: str | None = None):
        """
        初始化嵌入服务

        Args:
            model_alias: 嵌入模型别名，为 None 时使用默认嵌入模型
        """
        from src.core.di import get_global_container

        container = get_global_container()
        self.llm_factory = container.get("llm_factory")
        self.model_alias = model_alias

    async def embed_text(self, text: str) -> list[float]:
        """
        生成文本嵌入向量

        Args:
            text: 输入文本

        Returns:
            嵌入向量（浮点数列表）
        """
        # 获取嵌入模型客户端
        if self.model_alias:
            client = self.llm_factory.get_client(self.model_alias)
        else:
            # 使用默认的嵌入模型
            client = self.llm_factory.get_default_client(purpose="embedding")

        # 如果客户端有 embed 方法，直接使用
        if hasattr(client, "embed"):
            result = await client.embed(text)
            return result

        # 使用 sentence-transformers 作为后备方案
        try:
            import numpy as np
            from sentence_transformers import SentenceTransformer

            # 使用轻量级的嵌入模型
            model = SentenceTransformer("all-MiniLM-L6-v2")

            # 生成嵌入向量
            embedding = model.encode(text, convert_to_tensor=False)

            # 转换为列表格式
            if isinstance(embedding, np.ndarray):
                return embedding.tolist()
            else:
                return list(embedding)

        except ImportError:
            # 如果没有 sentence-transformers，使用简单的哈希嵌入
            import hashlib
            import struct

            # 使用文本哈希生成固定维度的向量
            text_hash = hashlib.sha256(text.encode()).digest()

            # 将哈希转换为 384 维向量（与 all-MiniLM-L6-v2 相同）
            embedding = []
            for i in range(0, len(text_hash), 4):
                chunk = text_hash[i : i + 4]
                if len(chunk) == 4:
                    value = struct.unpack("f", chunk)[0]
                    embedding.append(float(value))

            # 补齐到 384 维
            while len(embedding) < 384:
                embedding.append(0.0)

            # 归一化向量
            import math

            norm = math.sqrt(sum(x * x for x in embedding))
            if norm > 0:
                embedding = [x / norm for x in embedding]

            return embedding[:384]

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """
        批量生成文本嵌入向量

        Args:
            texts: 输入文本列表

        Returns:
            嵌入向量列表
        """
        # 批量处理，提升性能
        embeddings = []
        for text in texts:
            embedding = await self.embed_text(text)
            embeddings.append(embedding)
        return embeddings

    @staticmethod
    def cosine_similarity(vec1: list[float], vec2: list[float]) -> float:
        """
        计算余弦相似度

        Args:
            vec1: 向量1
            vec2: 向量2

        Returns:
            相似度得分（0-1）
        """
        import math

        # 计算点积
        dot_product = sum(a * b for a, b in zip(vec1, vec2, strict=False))

        # 计算向量长度
        norm1 = math.sqrt(sum(a * a for a in vec1))
        norm2 = math.sqrt(sum(b * b for b in vec2))

        # 避免除以零
        if norm1 == 0 or norm2 == 0:
            return 0.0

        return dot_product / (norm1 * norm2)
