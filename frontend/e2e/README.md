# AI Agent 系统 - 端到端测试指南

## 概述

本测试套件使用 Playwright 进行浏览器自动化测试，确保 AI Agent 系统的各个页面和功能正常工作。

## 前置要求

1. **安装依赖**
   ```bash
   npm install
   ```

2. **安装 Playwright 浏览器**
   ```bash
   npx playwright install
   ```

3. **启动开发服务器**
   ```bash
   # 终端 1：启动前端
   cd frontend
   npm run dev

   # 终端 2：启动后端（如果需要）
   cd ../src
   python -m uvicorn main:app --reload
   ```

## 测试结构

```
e2e/
├── helpers.ts              # 测试辅助函数
├── global-setup.ts         # 全局设置
├── global-teardown.ts      # 全局清理
├── login.spec.ts           # 登录页面测试
├── dashboard.spec.ts       # 仪表板页面测试
├── session.spec.ts         # 会话页面测试
├── settings.spec.ts        # 设置页面测试
├── run-tests.ts            # 综合测试
└── README.md              # 本文档
```

## 运行测试

### 1. 运行所有测试
```bash
npm run test:e2e
```

### 2. 运行特定测试文件
```bash
npx playwright test login.spec.ts
```

### 3. 调试模式（显示浏览器窗口）
```bash
npm run test:e2e:headed
```

### 4. 调试模式（逐步执行）
```bash
npm run test:e2e:debug
```

### 5. UI 模式（交互式测试）
```bash
npm run test:e2e:ui
```

## 测试覆盖范围

### 1. 登录页面 (login.spec.ts)
- ✅ 页面元素显示
- ✅ 表单验证
- ✅ 登录成功/失败
- ✅ 密码可见性切换
- ✅ 记住登录状态
- ✅ 注册页面跳转

### 2. 仪表板页面 (dashboard.spec.ts)
- ✅ 页面布局显示
- ✅ 侧边栏导航
- ✅ 统计卡片
- ✅ 最近活动
- ✅ 用户菜单
- ✅ 搜索功能
- ✅ 响应式布局
- ✅ 创建新会话

### 3. 会话页面 (session.spec.ts)
- ✅ 消息列表显示
- ✅ 发送消息
- ✅ 换行输入
- ✅ Agent 执行状态
- ✅ 执行图显示
- ✅ 暂停/恢复执行
- ✅ 停止执行
- ✅ 会话历史
- ✅ 清空消息
- ✅ 导出会话
- ✅ 文件上传

### 4. 设置页面 (settings.spec.ts)
- ✅ 设置标签页切换
- ✅ LLM 配置管理
- ✅ API 配置管理
- ✅ 工具配置管理
- ✅ 添加/编辑/删除配置
- ✅ 保存配置更改
- ✅ 启用/禁用工具
- ✅ 重置为默认配置
- ✅ 导入/导出配置
- ✅ 配置搜索

### 5. 综合测试 (run-tests.ts)
- ✅ 完整用户流程
- ✅ 响应式布局
- ✅ 性能测试
- ✅ 可访问性测试

## 测试报告

### 查看 HTML 报告
```bash
npm run test:e2e:report
```

### 查看测试结果
测试完成后，结果将保存在：
- `test-results/` - 截图和视频
- `playwright-report/` - HTML 报告
- `test-results.json` - JSON 格式结果
- `junit-results.xml` - JUnit 格式结果

## 截图目录

测试过程中的截图将保存在 `test-results/screenshots/` 目录：
- `login-page.png` - 登录页面
- `dashboard-page.png` - 仪表板
- `session-page.png` - 会话页面
- `settings-page.png` - 设置页面
- `dashboard-*.png` - 各种仪表板功能截图
- `session-*.png` - 各种会话功能截图
- `settings-*.png` - 各种设置功能截图

## 编写新测试

### 1. 创建测试文件
```typescript
import { test, expect } from '@playwright/test';
import { login, takeScreenshot } from './helpers';

test.describe('新功能测试', () => {
  test.beforeEach(async ({ page }) => {
    await login(page);
  });

  test('测试某个功能', async ({ page }) => {
    await page.goto('/new-page');

    // 你的测试代码
    await expect(page.locator('h1')).toBeVisible();
    await takeScreenshot(page, 'new-page-feature');
  });
});
```

### 2. 使用辅助函数

#### 2.1 登录和认证
```typescript
// 快速登录（使用测试账号）
await quickLogin(page);

// 使用自定义账号登录
await login(page, 'username', 'password');

// 通过 API 登录（绕过 UI，更快）
await loginViaAPI(page, 'username', 'password');

// 登出并清理状态
await logoutAndCleanup(page);
```

#### 2.2 网络请求监听
```typescript
// 监听 API 请求
const request = await waitForAPI(page, '/api/sessions', 'POST');

// 监听多个 API 请求（并行）
const requests = await waitForMultipleAPIs(page, [
  { endpoint: '/api/sessions', method: 'POST' },
  { endpoint: '/api/messages', method: 'POST' }
]);

// 监听 API 响应
const response = await waitForAPIResponse(page, '/api/users');

// 验证 API 状态码
await verifyAPIStatus(page, '/api/users', 200);

// 获取 API 响应数据
const data = await getAPIData<User>(page, '/api/users');
```

#### 2.3 状态记录和比较
```typescript
// 记录页面状态
const beforeState = await recordState(page, {
  username: 'input[name="username"]',
  submitButton: 'button[type="submit"]',
  errorMessage: '.error-message'
});

console.log(beforeState.username.visible); // true/false
console.log(beforeState.username.value);   // 输入框的值

// 记录并等待状态变化
const change = await waitForStateChange(page, '.status-text');

// 比较两个状态的差异
const afterState = await recordState(page, { username: 'input[name="username"]' });
const diff = compareStates(beforeState, afterState);

// 记录元素数量
const count = await recordElementCount(page, '.item');

// 验证元素数量变化
await verifyElementCountChanged(page, '.item', count, 'increase');
```

#### 2.4 会话管理
```typescript
// 创建会话（使用默认数据）
const session = await createSession(page);

// 创建会话（使用自定义数据）
const session = await createSession(page, {
  name: '测试会话',
  description: '这是一个测试会话',
  agentId: 'agent-123'
});

// 发送消息
await sendMessage(page, session.id, '你好，AI');

// 等待 AI 响应
const response = await waitForAIResponse(page, 30000);

// 获取会话中的所有消息
const messages = await getAllMessages(page);
```

#### 2.5 主题切换
```typescript
// 获取当前主题
const theme = await getCurrentTheme(page);

// 切换主题
await switchTheme(page, 'dark');

// 验证主题设置
await verifyTheme(page, 'dark');

// 等待主题切换动画完成
await waitForThemeTransition(page);

// 获取主题颜色
const colors = await getThemeColors(page, 'light');
```

#### 2.6 其他常用辅助函数
```typescript
// 截图
await takeScreenshot(page, 'feature-name');

// 检查 Toast 消息
await checkToast(page, '操作成功');

// 等待 Toast 消息
await waitForToast(page, '保存成功');
await waitForSuccessMessage(page);
await waitForErrorMessage(page);

// 填写表单
await fillForm(page, {
  'input[name="username"]': 'testuser',
  'input[name="email"]': 'test@example.com',
});

// 等待并点击
await waitAndClick(page, 'button.submit');

// 等待元素出现/消失
await waitForElement(page, '.modal');
await waitForElementRemoved(page, '.loading');

// 清空并填写输入框
await clearAndFill(page, 'input[name="name"]', 'new value');

// 选择下拉选项
await selectDropdown(page, '.dropdown-trigger', '选项名称');

// 上传文件
await uploadFile(page, 'input[type="file"]', '/path/to/file');

// 验证 URL
await verifyURL(page, '/dashboard');

// 存储状态管理
const token = await getStorageState(page, 'token');
await setStorageState(page, { token: 'xxx', user: 'xxx' });
```

## 故障排查

### 问题：测试失败，提示无法连接到服务器
**解决方案**：确保开发服务器正在运行
```bash
npm run dev
```

### 问题：元素未找到
**解决方案**：
1. 检查页面是否完全加载
2. 增加等待时间
3. 使用更精确的选择器

### 问题：测试超时
**解决方案**：
1. 增加 `test.setTimeout()` 时间
2. 检查网络请求是否完成
3. 使用 `page.waitForLoadState('networkidle')`

### 问题：浏览器未安装
**解决方案**：
```bash
npx playwright install
```

## 最佳实践

1. **使用 Page Object Model**：将页面选择器封装成类
2. **添加等待**：使用 `waitForSelector` 等待元素
3. **截图**：在关键步骤添加截图便于调试
4. **独立测试**：每个测试应该独立运行
5. **清理数据**：测试后清理创建的数据
6. **使用 data-testid**：添加专用测试属性提高稳定性

## 持续集成

在 CI/CD 环境中运行：

```yaml
# .github/workflows/e2e.yml
- name: Install dependencies
  run: npm install

- name: Install Playwright browsers
  run: npx playwright install --with-deps

- name: Run E2E tests
  run: npm run test:e2e

- name: Upload test results
  if: always()
  uses: actions/upload-artifact@v3
  with:
    name: playwright-report
    path: playwright-report/
```

## 更多资源

- [Playwright 官方文档](https://playwright.dev)
- [最佳实践指南](https://playwright.dev/docs/best-practices)
- [测试指南](https://playwright.dev/docs/intro)
