# 测试规范

---

## 1. 测试分类

### 1.1 测试金字塔

| 层级 | 测试类型 | 占比 | 特点 |
|------|---------|------|------|
| 底层 | **单元测试** | 70% | 快速、隔离、独立 |
| 中层 | **集成测试** | 20% | 验证模块间协作 |
| 顶层 | **E2E 测试** | 10% | 验证完整用户流程 |

### 1.2 各层测试定义

#### 单元测试

| 特征 | 说明 |
|------|------|
| 测试粒度 | 函数、方法、类级别 |
| 测试范围 | 单个单元，与外部依赖隔离 |
| 运行速度 | 毫秒级 |
| 依赖 | 使用 Mock 隔离外部依赖 |

```python
import pytest

def test_calculate_discount():
    result = calculate_discount(100, 0.2)
    assert result == 20

def test_user_service_get_user(mocker):
    mock_repo = mocker.Mock()
    mock_repo.get_by_id.return_value = User(id=1, name="Test")
    service = UserService(user_repo=mock_repo)
    user = service.get_user(1)
    assert user.id == 1
    mock_repo.get_by_id.assert_called_once_with(1)
```

#### 集成测试

| 特征 | 说明 |
|------|------|
| 测试粒度 | 模块、服务级别 |
| 测试范围 | 多个单元协作，真实依赖 |
| 运行速度 | 秒级 |
| 依赖 | 使用 Testcontainers 或内存数据库 |

```python
@pytest.fixture
def db_session(db_engine):
    Session = sessionmaker(bind=db_engine)
    session = Session()
    yield session
    session.rollback()
    session.close()

def test_user_repository_create(db_session):
    repo = UserRepository(db_session)
    user = repo.create(User(name="Test", email="test@example.com"))
    assert user.id is not None
```

#### E2E 测试

| 特征 | 说明 |
|------|------|
| 测试粒度 | 整个应用 |
| 测试范围 | 从 UI 到数据库的真实用户流程 |
| 运行速度 | 分钟级 |
| 依赖 | 真实服务、真实浏览器 |

---

## 2. 命名规范

### 2.1 pytest 命名约定

| 元素 | 规范 | 示例 |
|------|------|------|
| 测试文件 | `test_{模块名}.py` | `test_user_service.py` |
| 测试类 | `Test{被测类名}` | `TestUserService` |
| 测试函数 | `test_{方法名}_{场景}_{预期}` | `test_create_user_success` |
| Fixture | `{功能}_{scope}` | `db_session_function` |

### 2.2 测试文件结构

```
tests/
├── unit/                    # 单元测试
├── integration/             # 集成测试
├── e2e/                     # E2E 测试
└── fixtures/               # 共享 Fixture
    ├── conftest.py
    └── factories.py
```

---

## 3. 覆盖要求

覆盖率标准具体指标见 coding_domain_rules.md 九、质量门禁。

### 3.1 覆盖优先级

| 优先级 | 模块/函数 | 覆盖要求 |
|-------|----------|---------|
| P0 | 核心业务逻辑 | 100% 分支覆盖 |
| P1 | 公共服务、工具函数 | 90% 分支覆盖 |
| P2 | 一般业务代码 | 80% 分支覆盖 |
| P3 | 异常处理、边界处理 | 关键路径覆盖 |

### 3.2 pytest-cov 使用

```bash
pytest --cov=src --cov-report=html --cov-report=term
pytest --cov=src --cov-fail-under=80
```

### 3.3 覆盖率配置

```toml
[tool.coverage.run]
source = ["src"]
omit = ["*/tests/*", "*/migrations/*"]

[tool.coverage.report]
show_missing = true
exclude_lines = ["pragma: no cover", "def __repr__", "raise NotImplementedError"]
```

---

## 4. Mock 策略

### 4.1 测试隔离原则

- 每个测试独立，不依赖执行顺序
- 数据库、API、文件系统等外部依赖必须 Mock
- 每个测试使用独立的测试数据
- 测试之间不共享可变状态

### 4.2 Mock 使用场景

| 场景 | Mock 对象 | 不 Mock 对象 |
|------|----------|-------------|
| 单元测试 | 数据库、API、文件系统 | 纯函数逻辑 |
| 集成测试 | 外部 API | 数据库（用测试数据库） |
| E2E 测试 | 无 | 所有真实依赖 |

### 4.3 pytest-mock 使用

```python
# Mock 类实例
def test_email_service_send(mocker):
    mock_sender = mocker.Mock()
    mock_sender.send.return_value = True
    service = EmailService(sender=mock_sender)
    result = service.send("test@example.com", "Hello")
    assert result is True
    mock_sender.send.assert_called_once()

# Mock 模块级函数
def test_payment_process(mocker):
    mocker.patch("app.services.payment.stripe.Charge.create", return_value={"status": "succeeded"})
    result = payment_process(1000, "card_123")
    assert result.status == "succeeded"

# Fixture 复用 Mock
@pytest.fixture
def mock_user_repo(mocker):
    mock = mocker.Mock()
    mock.get_by_id.return_value = User(id=1, name="Test")
    return mock
```

### 4.4 Mock 最佳实践

- Mock 接口而非实现
- 验证调用参数（`assert_called_once_with`）
- 清理 Mock 状态（`mocker.reset_mock()`）
- 集成测试用真实依赖，避免过度 Mock

---

## 5. 测试数据管理

### 5.1 测试数据原则

- 每个测试创建自己的数据
- 使用固定种子保证可重复
- 只创建测试必需的数据
- 测试数据显式创建

### 5.2 Fixture 策略

```python
# Factory 模式
class UserFactory(Factory):
    class Meta:
        model = User
    name = "Test User"
    email = Factory(lambda: f"user_{uuid.uuid4()}@example.com")

# 事务回滚（推荐）
@pytest.fixture
def db_session():
    session = db_session_maker()
    transaction = session.begin_nested()
    yield session
    transaction.rollback()
    session.close()
```

### 5.3 常用测试数据

| 数据类型 | 示例 | 用途 |
|---------|------|------|
| 用户 | `test_user`, `admin_user` | 用户相关测试 |
| 金额 | `0`, `0.01`, `999999.99` | 边界值测试 |
| 字符串 | `""`, `"a"`, `"x"*1000` | 边界值测试 |

---

## 6. 前端 E2E 测试规范

### 6.1 Playwright 配置

```typescript
// playwright.config.ts
export default defineConfig({
  testDir: './tests/e2e',
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  use: {
    baseURL: 'http://localhost:3000',
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
  },
  projects: [
    { name: 'chromium', use: { ...devices['Desktop Chrome'] } },
    { name: 'firefox', use: { ...devices['Desktop Firefox'] } },
  ],
});
```

### 6.2 页面对象模式

```typescript
export class LoginPage {
  readonly usernameInput: Locator;
  readonly submitButton: Locator;

  constructor(page: Page) {
    this.usernameInput = page.getByTestId('username-input');
    this.submitButton = page.getByTestId('login-submit');
  }

  async login(username: string, password: string) {
    await this.usernameInput.fill(username);
    await this.submitButton.click();
  }
}
```

### 6.3 E2E 测试最佳实践

| 实践 | 说明 |
|------|------|
| 使用 `data-testid` | 避免依赖 CSS/结构选择器 |
| 使用显式等待 | `waitFor` 而非固定 sleep |
| 测试隔离 | 每个测试独立 |
| 页面对象封装 | 封装页面元素和操作 |
| 失败重试 | CI 环境配置重试 |

---

## 7. 禁止行为

### 7.1 测试设计

| 禁止行为 | 替代方案 |
|----------|----------|
| 测试实现细节 | 测试行为而非实现 |
| 断言过多 | 单一职责断言 |
| 测试无断言 | 必须有明确断言 |
| 顺序依赖测试 | 测试独立可并行 |
| 时间相关测试不等待 | 使用真实时间或控制时间 |

### 7.2 Mock

| 禁止行为 | 替代方案 |
|----------|----------|
| Mock 所有依赖 | 只 Mock 外部依赖 |
| 不验证 Mock 调用 | 验证调用参数和次数 |
| 全局 Mock | 本地化 Mock |
| Mock 私有方法 | 通过公共接口测试 |
| Mock 返回随机值 | 返回固定值 |

### 7.3 E2E 测试

| 禁止行为 | 替代方案 |
|----------|----------|
| 大量 E2E 测试 | 更多单元测试 |
| E2E 测试复杂逻辑 | 逻辑在单元测试覆盖 |
| 使用 CSS 选择器 | 使用 `data-testid` |
| 固定 sleep | 使用 `waitFor` |
| 不清理测试数据 | 测试后清理数据 |

### 7.4 数据管理

| 禁止行为 | 替代方案 |
|----------|----------|
| 使用生产数据 | 使用测试数据 |
| 硬编码邮箱/ID | 使用 Factory 生成 |
| 测试间共享数据 | 每个测试独立数据 |
| 不清理脏数据 | 测试后清理 |

---

## 8. 意图测试（Intent Testing）

测试必须编码 WHY——为什么这个行为重要，不只是 WHAT——输出是什么。

### 8.1 核心原则

| 测试类型 | 验证内容 | 示例 |
|----------|---------|------|
| 行为测试 | 验证"输入 X 输出 Y" | `assert add(2, 3) == 5` |
| 意图测试 | 验证"行为背后的业务意图" | `assert discount_price < original_price` |

### 8.2 实践要求

- 测试命名应反映业务意图
- 测试注释应说明"为什么这个行为重要"
- 关键业务规则的测试应包含意图描述注释
