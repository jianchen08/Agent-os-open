# Python 类型注解规范

## 一、基本类型注解

```python
# 函数签名必须完整注解
def process(data: str, max_len: int = 100) -> dict[str, Any]:
    ...

# 使用现代语法（Python 3.10+）
x: str | None = None          # 而非 Optional[str]
items: list[str] = []          # 而非 List[str]
mapping: dict[str, int] = {}   # 而非 Dict[str, int]
pair: tuple[str, int] = ("", 0) # 而非 Tuple[str, int]
```

---

## 二、复杂类型

| 场景 | 推荐写法 | 避免写法 |
|------|---------|---------|
| 可选值 | `str \| None` | `Optional[str]` |
| 联合类型 | `str \| int` | `Union[str, int]` |
| 字典 | `dict[str, Any]` | `Dict[str, Any]` |
| 列表 | `list[str]` | `List[str]` |
| 可调用 | `Callable[[str], int]` | - |
| 类型变量 | `TypeVar` | `Any` |

---

## 三、类型注解原则

1. **公共 API 必须注解**：所有公共函数/方法的参数和返回值
2. **避免 `Any`**：优先使用具体类型或 `Any` 的约束版本
3. **使用 `from __future__ import annotations`**：放在文件顶部，支持前向引用
4. **`Self` 类型**：使用 `from typing import Self` 而非字符串自引用
5. **泛型**：使用 `TypeVar` 或 `Generic` 而非 `Any`

---

## 四、dataclass 类型注解

```python
from __future__ import annotations
from dataclasses import dataclass, field

@dataclass
class AgentConfig:
    """Agent 配置。"""
    name: str = ""
    tool_ids: list[str] = field(default_factory=list)
    max_retries: int = 3
    metadata: dict[str, Any] = field(default_factory=dict)
```

---

## 五、异步函数注解

```python
async def fetch_data(url: str, *, timeout: int = 30) -> bytes:
    """异步函数也必须注解返回类型。"""
    ...
```

---

## 六、禁止行为

- 禁止在注解中使用字符串前向引用（用 `from __future__ import annotations` 代替）
- 禁止对 `self` / `cls` 加返回类型注解
- 禁止使用已废弃的 `typing` 别名（如 `List`, `Dict`, `Tuple`）
- 禁止在类型注解中使用 `type: ignore` 掩盖真实问题
