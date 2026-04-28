# Code Quality Skill - 代码质量检查技能

## 概述

代码质量检查技能，集成 LSP 诊断、静态分析（mypy/ruff）和测试覆盖率验证。通过 5 步质量闭环流程，确保代码符合质量标准后交付。

## 适用场景

当需要验证以下场景时激活此技能：

- **代码生成验证**：AI 生成的代码需要质量检查后交付
- **代码修改验证**：代码变更后需要验证类型安全和规范符合性
- **测试覆盖验证**：新增功能需要验证测试覆盖率达标
- **持续集成**：作为 CI/CD 流程中的质量门禁
- **代码审查辅助**：辅助人工 code review，发现潜在问题

## 引用工具

| 工具名称 | 路径/类型 | 用途 |
|---------|-----------|------|
| lsp_diagnostics | 系统内置工具 | LSP 语言服务协议诊断，实时检查代码错误 |
| bash_execute | 系统内置工具 | Shell 命令执行，用于运行 mypy/ruff/pytest |

### 工具能力说明

#### lsp_diagnostics

提供实时的语言服务器诊断，检测代码中的错误、警告和信息性问题。

**调用方式**：
```python
lsp_diagnostics(file_path="src/auth/login.py")
```

**返回格式**：
```json
{
  "diagnostics": [
    {
      "severity": "error",
      "file": "src/auth/login.py",
      "line": 42,
      "column": 10,
      "message": "Undefined variable 'user_name'",
      "code": "E0602"
    }
  ]
}
```

#### bash_execute

执行 Shell 命令，用于运行静态分析工具和测试框架。

**调用方式**：
```python
bash_execute(command="mypy --strict src/auth/login.py")
```

## 质量闭环流程

### 5 步闭环流程图

```
┌─────────────────────────────────────────────────────────────┐
│                      质量闭环流程                            │
│                                                             │
│   ┌──────────┐    ┌──────────┐    ┌──────────┐            │
│   │ 步骤 1   │───▶│ 步骤 2   │───▶│ 步骤 3   │            │
│   │ LSP诊断  │    │ 类型检查 │    │ 代码规范 │            │
│   └──────────┘    └──────────┘    └──────────┘            │
│        │                                 │                   │
│        │         ┌──────────┐            │                   │
│        │         │ 步骤 4   │            │                   │
│        │         │ 测试执行  │            │                   │
│        │         └──────────┘            │                   │
│        │              │                  │                   │
│        │              ▼                  │                   │
│        │         ┌──────────┐            │                   │
│        └────────▶│ 步骤 5   │◀───────────┘                   │
│                  │ 修复验证 │                                │
│                  └──────────┘                                │
│                       │                                      │
│              (问题存在? → 返回步骤 1)                          │
│              (无问题 → 流程结束)                              │
└─────────────────────────────────────────────────────────────┘
```

### 步骤 1：LSP 诊断

调用 lsp_diagnostics 检查代码文件，获取错误和警告列表。

**执行命令**：
```python
lsp_diagnostics(file_path="src/auth/login.py")
```

**分析维度**：

| 严重级别 | 含义 | 处理优先级 |
|----------|------|------------|
| error | 语法错误、类型错误等致命问题 | P0 - 必须修复 |
| warning | 潜在问题、代码异味 | P1 - 建议修复 |
| info | 优化建议、信息提示 | P2 - 可选处理 |

**结果分类**：
```python
errors = [d for d in diagnostics if d["severity"] == "error"]
warnings = [d for d in diagnostics if d["severity"] == "warning"]
infos = [d for d in diagnostics if d["severity"] == "info"]
```

### 步骤 2：类型检查

通过 mypy 执行严格的类型检查，验证类型注解的正确性。

**执行命令**：
```bash
mypy --strict --ignore-missing-imports src/auth/login.py
```

**输出示例**：
```
src/auth/login.py:42: error: Item "None" of "User | None" has no attribute "name"  [attr-defined]
src/auth/login.py:58: note: Revealed type is "builtins.str"
src/auth/login.py:73: error: Argument 1 to "validate" has incompatible type "int"; expected "str"  [arg-type]
```

**错误代码解析**：

| 错误代码 | 含义 | 修复方向 |
|----------|------|----------|
| attr-defined | 属性未定义或为 None | 添加空值检查或修复类型 |
| arg-type | 参数类型不匹配 | 检查参数类型注解 |
| union-attr | 联合类型属性访问 | 使用类型 narrowing |
| type-ignore | 类型忽略注释 | 审查是否合理，优先修复而非忽略 |

**配置说明**：
- `--strict`: 启用所有严格检查选项
- `--ignore-missing-imports`: 忽略第三方库类型缺失警告

### 步骤 3：代码规范检查

通过 ruff 检查代码规范和潜在问题。

**执行命令**：
```bash
ruff check src/auth/login.py
```

**输出示例**：
```
src/auth/login.py:42:5: E501 Line too long (85 > 79 characters)
src/auth/login.py:58:10: F401 'os.path' imported but unused
src/auth/login.py:73:15: E302 expected 2 blank lines, found 1
src/auth/login.py:90:2: E501 Line too long (120 > 79 characters)
```

**问题分类**：

| 类型 | 前缀 | 说明 | 是否可自动修复 |
|------|------|------|----------------|
| E/F | Ruff 规则 | 代码错误和警告 | 部分可修复 |
| F401 | 未使用导入 | import 但未使用 | 可自动修复 |
| E501 | 行长度 | 超过最大行长度 | 可自动修复 |
| E302 | 空白行 | 缺少必要空白行 | 可自动修复 |

**自动修复**：
```bash
ruff check --fix src/auth/login.py
```

### 步骤 4：测试执行与覆盖率

通过 pytest 执行测试并收集覆盖率报告。

**执行命令**：
```bash
pytest --cov=src/auth --cov-report=term-missing --cov-report=html tests/
```

**输出示例**：
```
---------- coverage: platform darwin, Python 3.11.0 ----------
Name                 Stmts   Miss  Branch BrPart  Cover
src/auth/__init__       12      0      0      0   100%
src/auth/login.py       85     15     20      4    82%
src/auth/logout.py       30     30      0      0     0%
TOTAL                  127     45     20      4    64%

---------- Missing Lines ----------
src/auth/login.py:42, 58, 73, 90
```

**覆盖率阈值**：

| 模块类型 | 覆盖率阈值 | 说明 |
|----------|------------|------|
| 核心逻辑 | 80% | 关键业务逻辑必须高覆盖 |
| 总体 | 60% | 整体项目最低要求 |

**失败测试分析**：
```bash
pytest --tb=short tests/test_login.py
```

### 步骤 5：修复验证

如果前序步骤发现问题，修复后重新执行闭环验证。

**重试策略**：

| 重试次数 | 处理方式 |
|----------|----------|
| 1-2 | 修复问题，重新运行全量检查 |
| 3 | 记录未解决问题，输出详细报告，终止流程 |
| >3 | 不再重试，标记为质量不达标 |

**闭环退出条件**：
- LSP 诊断：无 error 级别问题
- mypy 检查：无 error 级别问题
- ruff 检查：无未修复的规范问题
- pytest：所有测试通过，覆盖率达到阈值

## 结果解析

### LSP 诊断结果解析

```python
def parse_lsp_diagnostics(result):
    """解析 LSP 诊断结果"""
    diagnostics = result.get("diagnostics", [])
    
    categorized = {
        "error": [],
        "warning": [],
        "info": []
    }
    
    for d in diagnostics:
        severity = d.get("severity", "info")
        categorized[severity].append({
            "file": d.get("file"),
            "line": d.get("line"),
            "column": d.get("column"),
            "message": d.get("message"),
            "code": d.get("code")
        })
    
    return categorized
```

### mypy 输出解析

```python
def parse_mypy_output(output):
    """解析 mypy 输出"""
    errors = []
    
    for line in output.split("\n"):
        if ": error:" in line:
            # 解析 error 行
            # 格式: file:line: error: message
            errors.append({
                "file": line.split(":")[0],
                "line": int(line.split(":")[1]),
                "message": ":".join(line.split(":")[3:]).strip()
            })
    
    return errors
```

### ruff 输出解析

```python
def parse_ruff_output(output):
    """解析 ruff 输出"""
    issues = []
    fixable = []
    unfixable = []
    
    for line in output.split("\n"):
        if ": error:" in line or ": warning:" in line:
            issues.append(line)
            
            # 检查是否可修复
            if "F401" in line or "E501" in line or "E302" in line:
                fixable.append(line)
            else:
                unfixable.append(line)
    
    return {
        "total": len(issues),
        "fixable": fixable,
        "unfixable": unfixable
    }
```

### pytest 输出解析

```python
def parse_pytest_output(result):
    """解析 pytest 输出"""
    coverage = result.get("coverage", {})
    
    # 检查覆盖率
    total_coverage = coverage.get("total", {}).get("percent", 0)
    
    # 检查失败测试
    failed_tests = []
    for test in result.get("tests", []):
        if test["outcome"] == "FAILED":
            failed_tests.append({
                "name": test["name"],
                "message": test.get("message", ""),
                "line": test.get("line")
            })
    
    return {
        "total_coverage": total_coverage,
        "failed_tests": failed_tests,
        "passed": len(failed_tests) == 0
    }
```

## 修复建议模板

### LSP 错误修复

| 错误代码 | 错误类型 | 修复建议 |
|----------|----------|----------|
| E0602 | 未定义变量 | 检查变量名拼写，确保已导入或定义 |
| E1126 | 无效索引 | 检查索引类型和范围 |
| E1137 | 属性不支持赋值 | 检查对象类型定义 |
| F821 | 未定义名称 | 添加导入或定义变量 |

**修复模板**：
```python
# 错误示例
result = user.name  # E0602: user 可能未定义

# 修复方案 1：添加检查
if user is not None:
    result = user.name

# 修复方案 2：使用可选链（Python 3.10+）
result = user.name if user else None
```

### mypy 类型错误修复

| 错误类型 | 修复建议 |
|----------|----------|
| attr-defined | 添加类型注解或空值检查 |
| arg-type | 修正参数类型或添加类型转换 |
| return-type | 确保返回值类型与声明一致 |
| union-attr | 使用 isinstance 或类型守卫 |

**修复模板**：
```python
# 错误示例
def get_user_name(user: Optional[User]) -> str:
    return user.name  # error: Item "None" has no attribute "name"

# 修复方案
def get_user_name(user: Optional[User]) -> str:
    if user is None:
        return ""
    return user.name
```

### ruff 规范问题修复

#### 可自动修复（推荐优先）

```bash
# 修复所有可自动修复的问题
ruff check --fix src/auth/login.py
```

| 规则代码 | 问题 | 自动修复 |
|----------|------|----------|
| F401 | 未使用的导入 | 移除导入语句 |
| E501 | 行太长 | 自动换行或调整宽度 |
| E302 | 缺少空白行 | 添加空白行 |
| E231 | 缺少空格 | 添加空格 |

#### 手动修复

```python
# E501: 拆分长行
def long_function_name(
    param1: Type1,
    param2: Type2,
    param3: Type3
) -> ReturnType:
    pass

# E701: 复合语句拆分为多行
if condition:
    do_something()
else:
    do_otherthing()
```

### 测试失败修复

| 失败类型 | 分析方法 | 修复建议 |
|----------|----------|----------|
| AssertionError | 检查断言条件是否正确 | 修正断言或修复代码 |
| Fixture not found | 检查 fixture 定义 | 添加或修复 fixture |
| ImportError | 检查导入路径 | 修正 import 语句 |
| Timeout | 检查异步操作 | 增加超时或优化代码 |

**修复模板**：
```python
# 失败测试示例
def test_login_success():
    result = login("user", "pass")
    assert result.name == "user"  # 失败：result 为 None

# 分析：login 函数返回 None
# 修复 login 函数：
def login(username, password):
    user = authenticate(username, password)
    if user is None:
        return None
    return user  # 返回 user 对象

# 或修复测试断言：
def test_login_success():
    result = login("user", "pass")
    assert result is not None
    assert result.name == "user"
```

## 配置项

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| 覆盖率阈值-核心逻辑 | 80% | 关键业务代码的最低覆盖要求 |
| 覆盖率阈值-总体 | 60% | 整个项目的最低覆盖要求 |
| 质量闭环最大重试次数 | 3 | 修复验证的最大循环次数 |
| mypy 严格模式 | 是 | 启用所有严格类型检查 |
| ruff 规则集 | 默认 | 使用 ruff 默认规则 |
| LSP 诊断级别过滤 | error, warning | 仅关注 error 和 warning |

### mypy 配置 (mypy.ini 或 pyproject.toml)

```ini
[mypy]
python_version = 3.11
strict = True
ignore_missing_imports = True
warn_redundant_casts = True
warn_unused_configs = True
disallow_untyped_defs = True
```

### ruff 配置 (pyproject.toml)

```toml
[tool.ruff]
line-length = 100
target-version = "py311"

[tool.ruff.lint]
select = ["E", "F", "W", "I"]
ignore = ["E501"]  # 行长度可自动修复
```

## 最佳实践

### 检查顺序优化

**推荐顺序**：LSP 诊断 → mypy → ruff → pytest

**原因**：
1. LSP 诊断最快（语言服务器实时检查）
2. mypy 类型检查次之
3. ruff 规范检查较快
4. pytest 执行最慢（涉及代码运行）

### 修复优先级

**P0 - 必须修复**：
- LSP error
- mypy error
- pytest 测试失败（核心功能）

**P1 - 强烈建议修复**：
- LSP warning
- ruff 可自动修复问题
- pytest 测试失败（非核心功能）
- 覆盖率未达标

**P2 - 可选优化**：
- LSP info
- ruff 手动修复问题
- 注释和文档优化

### 修复后验证

**每轮修复后必须重新运行全量检查**：
```
步骤 1 → 步骤 2 → 步骤 3 → 步骤 4 → 步骤 5
   ↑                                        │
   └────────────────────────────────────────┘
            （有问题时返回步骤 1）
```

### 覆盖率分析流程

```
覆盖率不达标
     │
     ▼
分析未覆盖行
     │
     ├── 核心逻辑未覆盖 ──→ 补充测试用例
     │
     ├── 异常处理未覆盖 ──→ 补充边界测试
     │
     └── 不可达代码 ──→ 评估是否可删除或添加 # type: ignore
```

### 质量闭环报告模板

```markdown
## 代码质量检查报告

### 检查概况
- 检查时间: {timestamp}
- 检查文件: {files}
- 检查结果: {pass/fail}

### LSP 诊断结果
- Error: {count}
- Warning: {count}
- Info: {count}

### 类型检查结果 (mypy)
- Error: {count}
- 类型注解完整度: {percentage}%

### 代码规范结果 (ruff)
- 可自动修复: {count}
- 需手动修复: {count}
- 已自动修复: {count}

### 测试覆盖率
- 核心逻辑覆盖: {percentage}%
- 总体覆盖: {percentage}%
- 状态: {pass/fail}

### 未解决问题
| 序号 | 问题 | 文件 | 行号 | 严重级别 | 状态 |
|------|------|------|------|----------|------|
| 1 | ... | ... | ... | error | 未修复 |
| 2 | ... | ... | ... | warning | 未修复 |

### 修复建议
{详细修复建议}
```
