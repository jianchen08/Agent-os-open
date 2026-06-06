# ============================================================
# 测试代码模板
# ============================================================
#
# 【测试代码模板是什么】
# 测试代码模板是 Agent 在编写测试代码时参考的标准化模板和规范文档。
# 它定义了测试文件结构、测试类编写模板、pytest 配置、命名规范以及覆盖率要求。
#
# 【测试代码模板的作用】
# 1. 统一风格 — 所有测试代码遵循相同的结构和命名规范
# 2. 提高效率 — 提供现成的模板，减少编写样板代码的时间
# 3. 保证质量 — 明确覆盖率要求，确保测试充分性
# 4. 便于维护 — 统一的命名和结构使测试代码易于理解和修改
#
# 【如何使用本模板】
# 1. 根据要测试的模块类型，选择对应的测试类模板
# 2. 复制模板代码到对应的测试文件中（遵循文件结构说明）
# 3. 替换占位符为实际的测试内容
# 4. 确保命名遵循规范，覆盖率满足要求
#
# 【适用场景】
# - 为新增模块编写单元测试
# - 为 API 端点编写接口测试
# - 为端到端流程编写集成测试
# - 任何需要编写 pytest 测试代码的场景
# ============================================================

# 测试代码模板

---

## 文件结构说明 [必填]

> 本章节说明测试文件的目录组织方式。
>
> 测试文件位于 `tests/` 目录下，按类型组织：
>
> | 目录 | 类型 | 说明 | 示例 |
> |------|------|------|------|
> | `tests/unit/` | 单元测试 | 测试单个函数、类或模块 | `tests/unit/test_auth.py` |
> | `tests/api/` | API 测试 | 测试 API 端点的请求/响应 | `tests/api/test_task_api.py` |
> | `tests/e2e/` | 端到端测试 | 测试完整业务流程 | `tests/e2e/test_task_flow.py` |
> | `tests/agents/` | Agent 测试 | 测试 Agent 行为和产出 | `tests/agents/test_research_agent.py` |
> | `tests/scripts/` | 测试脚本 | 辅助调试和数据库操作的脚本 | `tests/scripts/db/check_db.py` |
>
> 前端测试：
>
> | 目录 | 类型 | 说明 |
> |------|------|------|
> | `frontend/src/test/` | 前端单元测试 | 组件和工具函数测试 |
> | `frontend/e2e/` | 前端 E2E 测试 | Playwright 页面交互测试 |
>
> 命名规则：
> - 测试文件必须以 `test_` 开头
> - 测试文件路径反映被测模块路径（如 `src/agents/research.py` → `tests/agents/test_research.py`）

**示例**：

```
tests/
├── unit/
│   ├── test_auth.py           # 对应 src/auth/
│   ├── test_task_manager.py   # 对应 src/task_manager/
│   └── test_utils.py          # 对应 src/utils/
├── api/
│   ├── test_task_api.py       # 对应 src/api/task.py
│   └── test_auth_api.py       # 对应 src/api/auth.py
├── e2e/
│   └── test_task_flow.py      # 端到端任务流程
├── agents/
│   └── test_research_agent.py # 对应 src/agents/research.py
└── scripts/
    ├── db/
    │   └── check_db.py        # 数据库检查脚本
    └── debug/
        └── check_sessions.py  # 调试脚本
```

---

## 测试类模板 [必填]

> 本章节提供标准化的 pytest 测试类模板。
>
> - 类型: 代码模板
> - 模板包含:
>   - 模块文档字符串
>   - 测试类定义
>   - pytest.fixture 初始化
>   - 正常场景测试方法（Arrange-Act-Assert 模式）
>   - 异常场景测试方法
> - 占位符说明:
>   | 占位符 | 说明 | 示例 |
>   |--------|------|------|
>   | `{测试模块名称}` | 被测模块的中文名称 | "认证模块" |
>   | `{测试描述}` | 测试文件的简要描述 | "测试用户认证相关功能" |
>   | `{ModuleName}` | 被测模块的英文名（PascalCase） | "Auth" |
>   | `{setup_code}` | 测试初始化代码 | "AuthService(db_session)" |
>   | `{case_name}` | 测试场景名称（snake_case） | "login_success" |
>   | `{test_description}` | 测试用例的中文描述 | "正常登录应返回有效 token" |
>   | `{input_data}` | 输入数据 | `{"username": "admin", "password": "123456"}` |
>   | `{expected_result}` | 期望的返回值 | `"valid_token_string"` |
>   | `{method}` | 被测方法名 | `"login"` |
>   | `{invalid_input}` | 无效输入数据 | `{"username": "", "password": ""}` |
>   | `{ExpectedError}` | 期望的异常类型 | `ValueError` |

**模板代码**：

```python
"""
{测试模块名称} - {测试描述}
"""
import pytest

class Test{ModuleName}:
    """{测试模块名称}测试类"""

    @pytest.fixture
    def setup(self):
        """测试初始化"""
        self.target = {setup_code}
        yield
        # 清理

    @pytest.mark.asyncio
    async def test_{case_name}(self, setup):
        """测试: {test_description}"""
        # Arrange - 准备测试数据
        input_data = {input_data}
        expected = {expected_result}

        # Act - 执行被测方法
        result = await self.target.{method}(input_data)

        # Assert - 验证结果
        assert result == expected

    @pytest.mark.asyncio
    async def test_{case_name}_error(self, setup):
        """测试: {test_description}（异常场景）"""
        # Arrange - 准备异常测试数据
        input_data = {invalid_input}

        # Act & Assert - 验证异常抛出
        with pytest.raises({ExpectedError}):
            await self.target.{method}(input_data)
```

**填写示例 — 认证模块测试**：

```python
"""
认证模块 - 测试用户认证相关功能
"""
import pytest
from src.auth.service import AuthService

class TestAuth:
    """认证模块测试类"""

    @pytest.fixture
    def setup(self, db_session):
        """测试初始化"""
        self.target = AuthService(db_session)
        yield
        # 清理

    @pytest.mark.asyncio
    async def test_login_success(self, setup):
        """测试: 正常登录应返回有效 token"""
        # Arrange
        input_data = {"username": "admin", "password": "correct_password"}
        expected_type = "str"

        # Act
        result = await self.target.login(input_data)

        # Assert
        assert isinstance(result.token, str)
        assert len(result.token) > 0

    @pytest.mark.asyncio
    async def test_login_invalid_password_error(self, setup):
        """测试: 密码错误应抛出异常"""
        # Arrange
        input_data = {"username": "admin", "password": "wrong_password"}

        # Act & Assert
        with pytest.raises(ValueError):
            await self.target.login(input_data)
```

---

## 测试配置模板 [必填]

> 本章节提供 pytest 的标准配置。
>
> - 类型: 配置文件片段
> - 配置方式: 在 `pyproject.toml` 中配置（推荐），或使用独立的 `pytest.ini`
> - 关键配置项说明:
>   | 配置项 | 类型 | 说明 | 推荐值 |
>   |--------|------|------|--------|
>   | testpaths | list[string] | 测试文件搜索路径 | `["tests"]` |
>   | asyncio_mode | string | asyncio 测试模式 | `"auto"`（自动识别异步测试） |
>   | markers | list[string] | 自定义标记 | `["asyncio: async test"]` |

**pyproject.toml 配置示例**：

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"
markers = [
    "asyncio: async test",
]
```

**常用运行命令**：

```bash
# 运行全部测试
pytest tests/ -v

# 运行指定目录的测试
pytest tests/api/ -v

# 运行指定文件的测试
pytest tests/unit/test_auth.py -v

# 运行指定测试方法
pytest tests/unit/test_auth.py::TestAuth::test_login_success -v

# 显示详细输出（含 print）
pytest tests/unit/test_auth.py -v -s

# 运行并生成覆盖率报告
pytest tests/ --cov=src --cov-report=term-missing
```

---

## 测试命名规范 [必填]

> 本章节定义测试代码的命名规范。
>
> - 类型: 规范表格
> - 命名规则:
>   | 类型 | 命名格式 | 说明 | 示例 |
>   |------|----------|------|------|
>   | 测试文件 | `test_{module}.py` | 以 test_ 开头，与被测模块同名 | `test_auth.py` |
>   | 测试类 | `Test{Feature}` | 以 Test 开头，PascalCase | `TestLogin` |
>   | 正常测试方法 | `test_{scenario}` | 以 test_ 开头，snake_case | `test_login_success` |
>   | 异常测试方法 | `test_{scenario}_error` | 以 _error 结尾 | `test_login_invalid_password_error` |
>   | 边界测试方法 | `test_{scenario}_boundary` | 以 _boundary 结尾 | `test_age_input_boundary` |
>   | 测试 fixture | `{feature}_setup` 或 `setup` | snake_case | `db_setup` |
> - 核心原则:
>   - 看名知义：仅看测试方法名就能理解测试场景
>   - 场景驱动：以业务场景而非技术实现命名
>   - 一致性：整个项目遵循相同的命名风格

**完整命名示例**：

```python
class TestTaskManager:
    # 正常场景
    def test_create_task_success(self): ...
    def test_get_task_by_id_success(self): ...

    # 异常场景
    def test_create_task_without_title_error(self): ...
    def test_get_task_nonexistent_id_error(self): ...

    # 边界场景
    def test_task_title_max_length_boundary(self): ...
```

---

## 覆盖率要求 [可选]

> 本章节定义不同模块类型的测试覆盖率要求。
>
> - 类型: 规范表格
> - 覆盖率要求:
>   | 模块类型 | 最低覆盖率 | 说明 | 检查方式 |
>   |----------|-----------|------|----------|
>   | 核心逻辑 | 90% | 业务关键路径，如认证、任务管理、Agent 调度 | `pytest --cov=src/core --cov-report=term-missing` |
>   | API 接口 | 85% | 请求/响应验证、参数校验、错误处理 | `pytest tests/api/ --cov=src/api` |
>   | 工具函数 | 80% | 边界条件覆盖、异常输入处理 | `pytest tests/unit/ --cov=src/utils` |
>   | 前端组件 | 70% | UI 交互、状态管理 | `cd frontend && npm run test:coverage` |
> - 计算方式: 语句覆盖率（Statement Coverage）
> - 检查命令: `pytest --cov=src --cov-report=term-missing`
> - 注意事项:
>   - 覆盖率是最低要求，建议尽量提高
>   - 不要为了覆盖率而写无意义的测试
>   - 重点覆盖核心业务逻辑和异常分支

**运行覆盖率检查**：

```bash
pytest tests/ --cov=src --cov-report=term-missing
pytest tests/ --cov=src --cov-report=html
```
