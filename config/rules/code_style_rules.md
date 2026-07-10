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

> 见「命名规范」。

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

## 五、禁止行为

> 通用禁止行为见「反模式清单」和「错误处理铁律」。以下为补充项：

- 禁止全局可变状态
- 禁止循环导入
- 禁止在 Windows 下使用 sed/awk 做文本替换（换行符和编码问题，优先用 `search_replace` 或 Python 脚本）
