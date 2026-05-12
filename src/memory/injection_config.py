"""
记忆注入配置

简化设计：
- 3种注入方式：full(全量)、summary(摘要)、retrieval(检索)
- 第1层 static_vars：长期保存的静态内容
- 第4层 dynamic_vars：每轮实时生成的动态内容
"""

from typing import Literal


class MemoryInjection:
    """记忆注入配置项"""

    def __init__(
        self,
        name: str,  # 记忆名称
        inject_type: Literal["full", "summary", "retrieval"],  # 注入方式
        top_k: int = 3,  # retrieval 时的数量
        query: str = "auto",  # retrieval 时的查询（auto=自动从消息提取）
    ):
        self.name = name
        self.inject_type = inject_type
        self.top_k = top_k
        self.query = query


class StaticVarInjection:
    """静态变量注入（第1层）"""

    def __init__(
        self,
        name: str,
        inject_type: Literal["full", "summary"] = "full",
    ):
        self.name = name
        self.inject_type = inject_type


class DynamicVarInjection:
    """动态变量注入（第4层）"""

    def __init__(
        self,
        type: Literal["timestamp", "session", "agent", "model", "memory"],
        name: str = "",  # memory 类型时需要
        inject_type: Literal["full", "summary", "retrieval"] = "full",
        top_k: int = 3,
    ):
        self.type = type
        self.name = name
        self.inject_type = inject_type
        self.top_k = top_k
