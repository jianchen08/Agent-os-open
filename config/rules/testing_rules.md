# 测试规范

---

## 概述

本规范定义了灵汐系统测试的标准规范和最佳实践，涵盖测试分类、命名规范、覆盖要求、Mock 策略、数据管理、E2E 测试和禁止行为。

### 来源标识说明

| 标识 | 含义 |
|------|------|
| [DOC] | 来自官方文档的推荐做法 |
| [BEST] | 社区公认的最佳实践 |
| [STD] | 来自国际标准、行业标准 |
| [TEAM] | 团队内部约定的规范 |
| [RESEARCH] | 基于系统调研的结论 |

---

## 1. 测试分类

### 1.1 测试金字塔

来源：[BEST] 测试金字塔理论（Mike Cohn）

测试金字塔是指导测试分层的基础理论，从下到上分为三层：

| 层级 | 测试类型 | 占比 | 特点 | 来源 |
|------|---------|------|------|------|
| 底层 | **单元测试** | 70% | 快速、隔离、独立 | [BEST] |
| 中层 | **集成测试** | 20% | 验证模块间协作 | [BEST] |
| 顶层 | **E2E 测试** | 10% | 验证完整用户流程 | [BEST] |

### 1.2 各层测试定义

来源：[BEST] 测试最佳实践

#### 单元测试

| 特征 | 说明 |
|------|------|
| 测试粒度 | 函数、方法、类级别 |
| 测试范围 | 单个单元，与外部依赖隔离 |
| 运行速度 | 毫秒级，数千个测试可并行 |
| 依赖 | 使用 Mock 隔离外部依赖 |
| 断言 | 关注返回值和行为 |

```python
# 单元测试示例
import pytest
from unittest.mock import Mock

def test_calculate_discount():
    # Arrange
    original_price = 100
    discount_rate = 0.2
    
    # Act
    result = calculate_discount(original_price, discount_rate)
    
    # Assert
    assert result == 20

def test_user_service_get_user(mocker):
    # Arrange
    mock_repo = mocker.Mock()
    mock_repo.get_by_id.return_value = User(id=1, name="Test")
    
    service = UserService(user_repo=mock_repo)
    
    # Act
    user = service.get_user(1)
    
    # Assert
    assert user.id == 1
    assert user.name == "Test"
    mock_repo.get_by_id.assert_called_once_with(1)
```

#### 集成测试

| 特征 | 说明 |
|------|------|
| 测试粒度 | 模块、服务级别 |
| 测试范围 | 多个单元协作，真实依赖（如数据库） |
| 运行速度 | 秒级，需启动测试环境 |
| 依赖 | 使用 Testcontainers 或内存数据库 |
| 断言 | 关注数据持久化和模块间交互 |

```python
# 集成测试示例
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

@pytest.fixture(scope="module")
def db_engine():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()

@pytest.fixture
def db_session(db_engine):
    Session = sessionmaker(bind=db_engine)
    session = Session()
    yield session
    session.rollback()
    session.close()

def test_user_repository_create(db_session):
    # 使用真实数据库，但数据回滚
    repo = UserRepository(db_session)
    user = repo.create(User(name="Test", email="test@example.com"))
    
    assert user.id is not None
    assert db_session.query(User).count() == 1
```

#### E2E 测试

| 特征 | 说明 |
|------|------|
| 测试粒度 | 整个应用 |
| 测试范围 | 从 UI 到数据库的真实用户流程 |
| 运行速度 | 分钟级，需启动完整环境 |
| 依赖 | 真实服务、真实浏览器 |
| 断言 | 关注用户体验和业务价值 |

### 1.3 测试分层策略

来源：[BEST] 测试策略最佳实践

```
测试金字塔原则：
├── 底层测试要多（单元测试）
│   └── 快速反馈，发现问题立即定位
├── 中层测试适中（集成测试）
│   └── 验证模块间接口和协作
└── 顶层测试要少（E2E 测试）
    └── 验证关键用户路径
```

---

## 2. 命名规范

### 2.1 pytest 社区约定

来源：[BEST] pytest 命名最佳实践

| 元素 | 规范 | 示例 | 来源 |
|------|------|------|------|
| 测试文件 | `test_{模块名}.py` | `test_user_service.py` | [BEST] |
| 测试类 | `Test{被测类名}` | `TestUserService` | [BEST] |
| 测试函数 | `test_{方法名}_{场景}_{预期}` | `test_create_user_success` | [BEST] |
| Fixture | `{功能}_{scope}` | `db_session_function` | [BEST] |

### 2.2 测试函数命名模式

来源：[BEST] pytest 社区约定

```
test_{function}_{scenario}_{expected_result}

模式说明：
- function: 被测试的函数/方法名
- scenario: 测试场景描述
- expected_result: 预期结果
```

| 场景 | 命名示例 |
|------|---------|
| 正常流程 | `test_create_user_success` |
| 异常情况 | `test_create_user_duplicate_email_raises_error` |
| 边界条件 | `test_calculate_discount_zero_price_returns_zero` |
| 空值处理 | `test_process_data_none_input_handles_gracefully` |

### 2.3 测试文件结构

来源：[BEST] 测试组织最佳实践

```
tests/
├── unit/                    # 单元测试
│   ├── test_user_service.py
│   └── test_order_processor.py
├── integration/             # 集成测试
│   ├── test_user_repository.py
│   └── test_payment_gateway.py
├── e2e/                     # E2E 测试
│   ├── test_login_flow.py
│   └── test_checkout_flow.py
└── fixtures/               # 共享 Fixture
    ├── conftest.py
    └── factories.py
```

---

## 3. 覆盖要求

### 3.1 覆盖率标准

来源：[BEST] 测试覆盖率最佳实践

| 指标 | 最低要求 | 目标 | 说明 |
|------|---------|------|------|
| **分支覆盖率** | 80% | 90% | 关键业务逻辑 |
| **函数覆盖率** | 90% | 100% | 所有公共函数 |
| **行覆盖率** | 80% | 90% | 核心模块 |

### 3.2 pytest-cov 使用

来源：[DOC] pytest-cov 官方文档

```bash
# 运行测试并生成覆盖率报告
pytest --cov=src --cov-report=html --cov-report=term

# 只检查覆盖率，不运行测试（配合 CI）
pytest --cov=src --cov-fail-under=80

# 生成多种格式报告
pytest --cov=src \
  --cov-report=term \
  --cov-report=html \
  --cov-report=xml \
  --cov-report=lcov
```

### 3.3 覆盖率配置

来源：[BEST] 覆盖率配置最佳实践

```toml
# .coveragerc 或 pyproject.toml
[tool.coverage.run]
source = ["src"]
omit = [
    "*/tests/*",
    "*/migrations/*",
    "*/__init__.py",
    "*/conftest.py",
]

[tool.coverage.report]
precision = 2
show_missing = true
skip_covered = false
exclude_lines = [
    "pragma: no cover",
    "def __repr__",
    "raise AssertionError",
    "raise NotImplementedError",
    "if __name__ == .__main__.:",
    "if TYPE_CHECKING:",
]
```

### 3.4 覆盖优先级

来源：[BEST] 测试覆盖策略

| 优先级 | 模块/函数 | 覆盖要求 | 原因 |
|-------|----------|---------|------|
| P0 | 核心业务逻辑 | 100% 分支覆盖 | 错误代价高 |
| P1 | 公共服务、工具函数 | 90% 分支覆盖 | 高复用 |
| P2 | 一般业务代码 | 80% 分支覆盖 | 合理资源分配 |
| P3 | 异常处理、边界处理 | 关键路径覆盖 | 难以触发 |

---

## 4. Mock 策略

### 4.1 测试隔离原则

来源：[BEST] 测试隔离最佳实践

| 原则 | 说明 | 来源 |
|------|------|------|
| 单元测试隔离 | 每个测试独立，不依赖执行顺序 | [BEST] |
| 外部依赖 Mock | 数据库、API、文件系统等外部依赖必须 Mock | [BEST] |
| 测试数据独立 | 每个测试使用独立的测试数据 | [BEST] |
| 无共享状态 | 测试之间不共享可变状态 | [BEST] |

### 4.2 Mock 使用场景

来源：[BEST] Mock 最佳实践

| 场景 | Mock 对象 | 不 Mock 对象 | 来源 |
|------|----------|-------------|------|
| 单元测试 | 数据库、API、文件系统 | 纯函数逻辑 | [BEST] |
| 集成测试 | 外部 API | 数据库（用测试数据库） | [BEST] |
| E2E 测试 | 无 | 所有真实依赖 | [BEST] |

### 4.3 pytest-mock 使用示例

来源：[DOC] pytest-mock 官方文档

```python
import pytest
from unittest.mock import Mock, patch, MagicMock

# 1. Mock 类实例
def test_email_service_send(mocker):
    mock_sender = mocker.Mock()
    mock_sender.send.return_value = True
    
    service = EmailService(sender=mock_sender)
    result = service.send("test@example.com", "Hello")
    
    assert result is True
    mock_sender.send.assert_called_once_with(
        to="test@example.com",
        subject="Hello"
    )

# 2. Mock 模块级函数
def test_payment_process(mocker):
    mocker.patch("app.services.payment.stripe.Charge.create", return_value={
        "id": "ch_123",
        "status": "succeeded"
    })
    
    result = payment_process(1000, "card_123")
    assert result.status == "succeeded"

# 3. Spy 监视真实调用
def test_cache_get_miss(mocker):
    real_get = mocker.patch("app.cache.RedisCache.get", wraps=real_get)
    
    cache = RedisCache()
    result = cache.get("key1")
    
    assert result is None
    assert real_get.call_count == 1

# 4. Fixture 复用 Mock
@pytest.fixture
def mock_user_repo(mocker):
    mock = mocker.Mock()
    mock.get_by_id.return_value = User(id=1, name="Test")
    return mock
```

### 4.4 Mock 最佳实践

来源：[BEST] Mock 反模式与最佳实践

| 最佳实践 | 说明 | 来源 |
|---------|------|------|
| Mock 接口而非实现 | 通过抽象接口 Mock | [BEST] |
| 验证调用参数 | 使用 `assert_called_once_with` | [BEST] |
| 清理 Mock 状态 | 使用 `mocker.reset_mock()` | [BEST] |
| 避免过度 Mock | 集成测试用真实依赖 | [BEST] |
| 使用 Spy 保留真实行为 | 需要验证调用但保留实现时 | [BEST] |

---

## 5. 测试数据管理

### 5.1 测试数据原则

来源：[BEST] 测试数据最佳实践

| 原则 | 说明 | 来源 |
|------|------|------|
| 测试数据独立 | 每个测试创建自己的数据 | [BEST] |
| 数据可预测 | 使用固定种子保证可重复 | [BEST] |
| 最小化数据 | 只创建测试必需的数据 | [BEST] |
| 显式优于隐式 | 测试数据显式创建 | [BEST] |

### 5.2 Fixture 策略

来源：[BEST] pytest Fixture 最佳实践

```python
import pytest
from factory import Factory
from app.models import User

# 1. 基础 Fixture
@pytest.fixture
def db_session():
    session = create_test_session()
    yield session
    session.rollback()

# 2. Factory 模式
class UserFactory(Factory):
    class Meta:
        model = User
    
    name = "Test User"
    email = Factory(lambda: f"user_{uuid.uuid4()}@example.com")
    role = "user"

# 3. 参数化 Fixture
@pytest.fixture(params=["admin", "user", "guest"])
def user_with_role(request):
    return User(role=request.param)

# 4. Session 级 Fixture（共享数据）
@pytest.fixture(scope="session")
def test_organization(db_engine):
    org = Organization(name="Test Org")
    db_engine.session.add(org)
    db_engine.session.commit()
    return org
```

### 5.3 测试数据清理

来源：[BEST] 测试数据管理

```python
# 1. 事务回滚（推荐）
@pytest.fixture
def db_session():
    session = db_session_maker()
    transaction = session.begin_nested()
    
    yield session
    
    transaction.rollback()
    session.close()

# 2. Fixture 清理
@pytest.fixture
def temp_file(tmp_path):
    file = tmp_path / "test.txt"
    file.write_text("test")
    yield file
    # Cleanup happens automatically with tmp_path

# 3. Class 级清理
@pytest.fixture(scope="class")
def setup_class_data():
    data = create_test_data()
    yield data
    cleanup_test_data(data)
```

### 5.4 常用测试数据

来源：[TEAM] 团队约定

| 数据类型 | 示例 | 用途 |
|---------|------|------|
| 用户 | `test_user`, `admin_user`, `inactive_user` | 用户相关测试 |
| 订单 | `pending_order`, `completed_order`, `cancelled_order` | 订单流程测试 |
| 金额 | `0`, `0.01`, `999999.99` | 边界值测试 |
| 字符串 | `""`, `"a"`, `"x"*1000` | 边界值测试 |

---

## 6. 前端 E2E 测试规范

### 6.1 Playwright 测试工具

来源：[RESEARCH] 系统调研结论 + [TOOL] Playwright 官方文档

| 特性 | 说明 | 来源 |
|------|------|------|
| 支持浏览器 | Chromium, Firefox, WebKit | [TOOL] |
| 自动等待 | 智能等待元素出现 | [TOOL] |
| 隔离执行 | 测试间完全隔离 | [TOOL] |
| 追踪查看器 | 记录测试执行过程 | [TOOL] |

### 6.2 Playwright 测试配置

来源：[DOC] Playwright 官方文档

```typescript
// playwright.config.ts
import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
  testDir: './tests/e2e',
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: process.env.CI ? 1 : undefined,
  reporter: 'html',
  
  use: {
    baseURL: 'http://localhost:3000',
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
  },
  
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
    {
      name: 'firefox',
      use: { ...devices['Desktop Firefox'] },
    },
    {
      name: 'webkit',
      use: { ...devices['Desktop Safari'] },
    },
  ],
});
```

### 6.3 页面对象模式

来源：[BEST] Playwright 最佳实践

```typescript
// pages/LoginPage.ts
import { Page, Locator, expect } from '@playwright/test';

export class LoginPage {
  readonly page: Page;
  readonly usernameInput: Locator;
  readonly passwordInput: Locator;
  readonly submitButton: Locator;
  readonly errorMessage: Locator;

  constructor(page: Page) {
    this.page = page;
    this.usernameInput = page.getByTestId('username-input');
    this.passwordInput = page.getByTestId('password-input');
    this.submitButton = page.getByTestId('login-submit');
    this.errorMessage = page.getByTestId('error-message');
  }

  async goto() {
    await this.page.goto('/login');
  }

  async login(username: string, password: string) {
    await this.usernameInput.fill(username);
    await this.passwordInput.fill(password);
    await this.submitButton.click();
  }

  async expectErrorVisible() {
    await expect(this.errorMessage).toBeVisible();
  }
}

// tests/e2e/login.spec.ts
import { test, expect } from '@playwright/test';
import { LoginPage } from '../../pages/LoginPage';

test.describe('Login Flow', () => {
  test('登录成功跳转到首页', async ({ page }) => {
    const loginPage = new LoginPage(page);
    await loginPage.goto();
    await loginPage.login('admin', 'password123');
    
    await expect(page).toHaveURL('/dashboard');
  });

  test('登录失败显示错误提示', async ({ page }) => {
    const loginPage = new LoginPage(page);
    await loginPage.goto();
    await loginPage.login('invalid', 'wrong');
    
    await loginPage.expectErrorVisible();
  });
});
```

### 6.4 E2E 测试最佳实践

来源：[BEST] E2E 测试最佳实践

| 实践 | 说明 | 来源 |
|------|------|------|
| 使用 `data-testid` | 避免依赖 CSS/结构选择器 | [BEST] |
| 使用显式等待 | 使用 `waitFor` 而非固定 sleep | [BEST] |
| 测试隔离 | 每个测试独立，不依赖顺序 | [BEST] |
| 页面对象封装 | 封装页面元素和操作 | [BEST] |
| 失败重试 | CI 环境配置重试 | [BEST] |

---

## 7. 禁止行为

### 7.1 测试设计禁止行为

来源：[BEST] 测试反模式

| 禁止行为 | 风险 | 替代方案 | 来源 |
|----------|------|----------|------|
| 禁止测试实现细节 | 过度耦合，破坏重构 | 测试行为而非实现 | [BEST] |
| 禁止断言过多 | 难以定位失败原因 | 单一职责断言 | [BEST] |
| 禁止测试无断言 | 测试无意义 | 必须有明确断言 | [BEST] |
| 禁止顺序依赖测试 | 测试不稳定 | 测试独立可并行 | [BEST] |
| 禁止时间相关测试不等待 | Flaky 测试 | 使用真实时间或控制时间 | [BEST] |

### 7.2 Mock 禁止行为

来源：[BEST] Mock 反模式

| 禁止行为 | 风险 | 替代方案 | 来源 |
|----------|------|----------|------|
| 禁止 Mock 所有依赖 | 测试无意义 | 只 Mock 外部依赖 | [BEST] |
| 禁止不验证 Mock 调用 | 测试无断言 | 验证调用参数和次数 | [BEST] |
| 禁止全局 Mock | 难以排查问题 | 本地化 Mock | [BEST] |
| 禁止 Mock 私有方法 | 实现细节耦合 | 通过公共接口测试 | [BEST] |
| 禁止 Mock 返回随机值 | 断言不稳定 | 返回固定值 | [BEST] |

### 7.3 E2E 测试禁止行为

来源：[BEST] E2E 测试反模式

| 禁止行为 | 风险 | 替代方案 | 来源 |
|----------|------|----------|------|
| 禁止大量 E2E 测试 | 运行时间长，维护成本高 | 更多单元测试 | [BEST] |
| 禁止 E2E 测试复杂逻辑 | 难以调试 | 逻辑在单元测试覆盖 | [BEST] |
| 禁止使用 CSS 选择器 | 结构变化导致失败 | 使用 `data-testid` | [BEST] |
| 禁止固定 sleep | 不稳定 | 使用 `waitFor` | [BEST] |
| 禁止不清理测试数据 | 环境污染 | 测试后清理数据 | [BEST] |

### 7.4 数据管理禁止行为

来源：[BEST] 测试数据反模式

| 禁止行为 | 风险 | 替代方案 | 来源 |
|----------|------|----------|------|
| 禁止使用生产数据 | 安全风险、数据污染 | 使用测试数据 | [BEST] |
| 禁止硬编码邮箱/ID | 数据冲突 | 使用 Factory 生成 | [BEST] |
| 禁止测试间共享数据 | 测试耦合 | 每个测试独立数据 | [BEST] |
| 禁止不清理脏数据 | 环境污染 | 测试后清理 | [BEST] |
| 禁止大体积测试数据 | 性能问题 | 最小化测试数据 | [BEST] |
