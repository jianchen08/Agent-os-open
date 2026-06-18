# 代码风格规范

## 一、通用编码规范

| 规则 | 说明 |
|------|------|
| 缩进 | 4 空格，禁止 Tab |
| 行宽 | 建议不超过 120 字符 |
| 编码 | UTF-8 |
| 换行符 | 与项目一致（Windows: CRLF, Linux/Mac: LF） |

---

## 二、命名规范

| 类型 | 风格 | 示例 |
|------|------|------|
| 模块 | snake_case | `data_loader.py` |
| 函数/方法 | snake_case | `def load_config():` |
| 类 | PascalCase | `class AgentConfig:` |
| 常量 | UPPER_SNAKE | `MAX_RETRIES = 3` |
| 私有属性 | _前缀 | `self._buffer` |
| 包 | snake_case | `pipeline/` |

---

## 三、Python 特定规范

- 使用 Python 3.10+ 语法（`from __future__ import annotations`）
- 类型注解覆盖所有公共函数签名
- Google 风格 docstring（Args / Returns / Raises）
- 使用 `pathlib.Path` 替代 `os.path`
- 使用 f-string 替代 `%` 和 `.format()`
- 异常处理使用具体异常类型，禁止裸 `except:`

---

## 四、文件结构

```python
"""模块文档字符串。"""

from __future__ import annotations

import 标准库
import 第三方库
import 项目内部模块

logger = logging.getLogger(__name__)

# 常量定义

class Foo:
    """类的文档字符串。"""

    def bar(self, x: int) -> str:
        """方法的文档字符串。

        Args:
            x: 参数说明

        Returns:
            返回值说明
        """
        ...

# 私有辅助函数
```

---

## 五、Windows 文本替换

- 优先用 `search_replace` 或 Python 脚本，禁止用 sed/awk（换行符和编码问题）
- 批量替换时先 dry_run 预览，一次性替换，始终指定 `encoding="utf-8"`

---

## 六、禁止行为

- 禁止硬编码配置值（使用配置文件或常量）
- 禁止忽略异常（空 `except` 块必须至少 `logger.debug`）
- 禁止全局可变状态
- 禁止循环导入
- 禁止在 Windows 下使用 sed/awk 做文本替换
