# E2E 测试辅助函数参考文档

本文档提供了 `helpers.ts` 中所有辅助函数的完整参考。

## 目录

- [登录和认证](#登录和认证)
- [网络请求监听](#网络请求监听)
- [状态记录和比较](#状态记录和比较)
- [会话管理](#会话管理)
- [主题相关](#主题相关)
- [表单交互](#表单交互)
- [消息和通知](#消息和通知)
- [导航验证](#导航验证)
- [元素操作](#元素操作)
- [存储管理](#存储管理)
- [WebSocket](#websocket)
- [数据库验证](#数据库验证)

---

## 登录和认证

### `login(page, username?, password?)`

执行完整的 UI 登录流程。

**参数**:
- `page: Page` - Playwright Page 对象
- `username?: string` - 用户名（默认使用测试账号）
- `password?: string` - 密码（默认使用测试账号）

**返回值**: `Promise<void>`

**示例**:
```typescript
await login(page);
await login(page, 'myuser', 'mypass');
```

---

### `quickLogin(page, username?, password?)`

快速登录，先检查是否已登录，避免重复登录。

**参数**:
- `page: Page` - Playwright Page 对象
- `username?: string` - 用户名（默认使用测试账号）
- `password?: string` - 密码（默认使用测试账号）

**返回值**: `Promise<void>`

**示例**:
```typescript
await quickLogin(page);
```

---

### `loginViaAPI(page, username?, password?)`

通过 API 直接登录，绕过 UI 操作，速度更快。

**参数**:
- `page: Page` - Playwright Page 对象
- `username?: string` - 用户名（默认使用测试账号）
- `password?: string` - 密码（默认使用测试账号）

**返回值**: `Promise<any>` - 返回登录响应数据

**示例**:
```typescript
const userData = await loginViaAPI(page);
console.log(userData.user);
```

---

### `logout(page)`

执行登出操作。

**参数**:
- `page: Page` - Playwright Page 对象

**返回值**: `Promise<void>`

**示例**:
```typescript
await logout(page);
```

---

### `logoutAndCleanup(page)`

登出并清理所有状态（localStorage、sessionStorage、cookies）。

**参数**:
- `page: Page` - Playwright Page 对象

**返回值**: `Promise<void>`

**示例**:
```typescript
await logoutAndCleanup(page);
```

---

## 网络请求监听

### `waitForAPI(page, endpoint, method?)`

监听特定的 API 请求。

**参数**:
- `page: Page` - Playwright Page 对象
- `endpoint: string` - API 端点（部分匹配）
- `method?: 'GET' | 'POST' | 'PUT' | 'DELETE' | 'PATCH'` - HTTP 方法（可选）

**返回值**: `Promise<Request>` - 返回请求对象

**示例**:
```typescript
// 监听任何方法的请求
const request = await waitForAPI(page, '/api/sessions');

// 只监听 POST 请求
const postRequest = await waitForAPI(page, '/api/sessions', 'POST');
```

---

### `waitForMultipleAPIs(page, requests)`

同时监听多个 API 请求。

**参数**:
- `page: Page` - Playwright Page 对象
- `requests: Array<{ endpoint: string; method?: string }>` - 请求配置数组

**返回值**: `Promise<Request[]>` - 返回请求对象数组

**示例**:
```typescript
const [req1, req2] = await waitForMultipleAPIs(page, [
  { endpoint: '/api/sessions', method: 'POST' },
  { endpoint: '/api/messages', method: 'POST' }
]);
```

---

### `waitForAPIResponse(page, urlPattern)`

监听 API 响应（已存在的函数）。

**参数**:
- `page: Page` - Playwright Page 对象
- `urlPattern: string | RegExp` - URL 匹配模式

**返回值**: `Promise<Response>` - 返回响应对象

---

### `verifyAPIStatus(page, urlPattern, expectedStatus)`

验证 API 响应状态码（已存在的函数）。

**参数**:
- `page: Page` - Playwright Page 对象
- `urlPattern: string` - URL 模式
- `expectedStatus: number` - 期望的状态码

**返回值**: `Promise<Response>`

---

### `getAPIData<T>(page, urlPattern)`

获取 API 响应数据（已存在的函数）。

**参数**:
- `page: Page` - Playwright Page 对象
- `urlPattern: string` - URL 模式

**返回值**: `Promise<T | null>`

---

## 状态记录和比较

### `recordState(page, selectors)`

记录页面元素的可见性、值和文本内容。

**参数**:
- `page: Page` - Playwright Page 对象
- `selectors: Record<string, string>` - 选择器对象，key 为名称，value 为选择器

**返回值**: `Promise<Record<string, { visible: boolean; value?: string; text?: string }>>`

**示例**:
```typescript
const state = await recordState(page, {
  username: 'input[name="username"]',
  submitButton: 'button[type="submit"]',
  errorMessage: '.error-message'
});

console.log(state.username.visible); // true/false
console.log(state.username.value);   // 输入框的值
console.log(state.submitButton.text); // 按钮文本
```

---

### `compareStates(beforeState, afterState)`

比较两个状态的差异。

**参数**:
- `beforeState: Record<string, any>` - 之前的状态
- `afterState: Record<string, any>` - 当前的状态

**返回值**: `Record<string, { before: any; after: any; changed: boolean }>`

**示例**:
```typescript
const before = await recordState(page, { count: '.item' });
await performAction(page);
const after = await recordState(page, { count: '.item' });
const diff = compareStates(before, after);

console.log(diff);
// {
//   count: {
//     before: { visible: true, text: '5' },
//     after: { visible: true, text: '6' },
//     changed: true
//   }
// }
```

---

### `waitForStateChange(page, selector, timeout?)`

等待元素内容发生变化。

**参数**:
- `page: Page` - Playwright Page 对象
- `selector: string` - 要监听的选择器
- `timeout?: number` - 超时时间（毫秒，默认 5000）

**返回值**: `Promise<{ before: string | null; after: string | null }>`

**示例**:
```typescript
const { before, after } = await waitForStateChange(page, '.status-text');
console.log(`状态从 "${before}" 变为 "${after}"`);
```

---

### `recordElementCount(page, selector)`

记录符合选择器的元素数量（已存在的函数）。

**参数**:
- `page: Page` - Playwright Page 对象
- `selector: string` - 选择器

**返回值**: `Promise<number>`

---

### `verifyElementCountChanged(page, selector, initialCount, expectedChange)`

验证元素数量变化（已存在的函数）。

**参数**:
- `page: Page` - Playwright Page 对象
- `selector: string` - 选择器
- `initialCount: number` - 初始数量
- `expectedChange: 'increase' | 'decrease' | number` - 期望的变化

**返回值**: `Promise<number>` - 当前数量

**示例**:
```typescript
const before = await recordElementCount(page, '.item');
// 执行操作...
await verifyElementCountChanged(page, '.item', before, 'increase');
// 或指定具体变化量
await verifyElementCountChanged(page, '.item', before, 2);
```

---

## 会话管理

### `createSession(page, sessionData?)`

创建新的会话。

**参数**:
- `page: Page` - Playwright Page 对象
- `sessionData?: { name?: string; description?: string; agentId?: string }` - 会话数据（可选）

**返回值**: `Promise<{ id: string | null; name: string; description: string; agentId?: string }>`

**示例**:
```typescript
// 使用默认数据
const session1 = await createSession(page);

// 使用自定义数据
const session2 = await createSession(page, {
  name: '测试会话',
  description: '这是一个测试会话',
  agentId: 'agent-123'
});
```

---

### `sendMessage(page, sessionId, content)`

向会话发送消息。

**参数**:
- `page: Page` - Playwright Page 对象
- `sessionId: string` - 会话 ID
- `content: string` - 消息内容

**返回值**: `Promise<{ sessionId: string; content: string; timestamp: string; success: boolean }>`

**示例**:
```typescript
await sendMessage(page, 'session-123', '你好，AI');
```

---

### `waitForAIResponse(page, timeout?)`

等待 AI 响应消息。

**参数**:
- `page: Page` - Playwright Page 对象
- `timeout?: number` - 超时时间（毫秒，默认 30000）

**返回值**: `Promise<string>` - AI 响应内容

**示例**:
```typescript
const response = await waitForAIResponse(page, 30000);
console.log('AI 响应:', response);
```

---

### `getAllMessages(page)`

获取会话中的所有消息。

**参数**:
- `page: Page` - Playwright Page 对象

**返回值**: `Promise<Array<{ role: string; content: string }>>`

**示例**:
```typescript
const messages = await getAllMessages(page);
console.log(`总共有 ${messages.length} 条消息`);
messages.forEach((msg, index) => {
  console.log(`${index + 1}. [${msg.role}]: ${msg.content}`);
});
```

---

## 主题相关

### `getCurrentTheme(page)`

获取当前主题（已存在的函数）。

**参数**:
- `page: Page` - Playwright Page 对象

**返回值**: `Promise<string>` - 当前主题（'light'、'dark' 或 'auto'）

---

### `switchTheme(page, theme)`

切换主题。

**参数**:
- `page: Page` - Playwright Page 对象
- `theme: 'light' | 'dark' | 'auto'` - 目标主题

**返回值**: `Promise<void>`

**示例**:
```typescript
await switchTheme(page, 'dark');
await verifyTheme(page, 'dark');
```

---

### `verifyTheme(page, expectedTheme)`

验证主题设置（已存在的函数）。

**参数**:
- `page: Page` - Playwright Page 对象
- `expectedTheme: string` - 期望的主题

**返回值**: `Promise<void>`

---

### `waitForThemeTransition(page)`

等待主题切换动画完成。

**参数**:
- `page: Page` - Playwright Page 对象

**返回值**: `Promise<void>`

**示例**:
```typescript
await switchTheme(page, 'dark');
await waitForThemeTransition(page);
// 现在可以安全地进行断言或截图
```

---

### `getThemeColors(page, theme)`

获取主题颜色配置。

**参数**:
- `page: Page` - Playwright Page 对象
- `theme: 'light' | 'dark'` - 主题名称

**返回值**: `Promise<{ background: string; foreground: string; primary: string; secondary: string }>`

**示例**:
```typescript
const colors = await getThemeColors(page, 'dark');
console.log('主题颜色:', colors);
```

---

### `getElementColor(page, selector, property)`

获取元素颜色（已存在的函数）。

**参数**:
- `page: Page` - Playwright Page 对象
- `selector: string` - 元素选择器
- `property: 'color' | 'backgroundColor'` - CSS 属性

**返回值**: `Promise<string>`

---

## 表单交互

### `fillForm(page, fields)`

批量填写表单（已存在的函数）。

**参数**:
- `page: Page` - Playwright Page 对象
- `fields: Record<string, string>` - 字段选择器和值的映射

**返回值**: `Promise<void>`

**示例**:
```typescript
await fillForm(page, {
  'input[name="username"]': 'testuser',
  'input[name="email"]': 'test@example.com',
  'textarea[name="description"]': '这是描述'
});
```

---

### `clearAndFill(page, selector, value)`

清空并填写输入框（已存在的函数）。

**参数**:
- `page: Page` - Playwright Page 对象
- `selector: string` - 输入框选择器
- `value: string` - 新值

**返回值**: `Promise<void>`

**示例**:
```typescript
await clearAndFill(page, 'input[name="name"]', '新值');
```

---

### `selectDropdown(page, triggerSelector, optionText)`

选择下拉选项（已存在的函数）。

**参数**:
- `page: Page` - Playwright Page 对象
- `triggerSelector: string` - 下拉框触发器选择器
- `optionText: string` - 选项文本

**返回值**: `Promise<void>`

**示例**:
```typescript
await selectDropdown(page, '.model-select', 'GPT-4');
```

---

## 消息和通知

### `checkToast(page, message)`

检查 Toast 消息（已存在的函数）。

**参数**:
- `page: Page` - Playwright Page 对象
- `message: string` - 期望的消息文本

**返回值**: `Promise<void>`

---

### `waitForToast(page, message?, timeout?)`

等待 Toast 消息出现。

**参数**:
- `page: Page` - Playwright Page 对象
- `message?: string` - 期望的消息文本（可选）
- `timeout?: number` - 超时时间（毫秒，默认 5000）

**返回值**: `Promise<void>`

**示例**:
```typescript
// 等待任意 Toast
await waitForToast(page);

// 等待特定消息
await waitForToast(page, '保存成功');
```

---

### `waitForSuccessMessage(page, timeout?)`

等待成功消息。

**参数**:
- `page: Page` - Playwright Page 对象
- `timeout?: number` - 超时时间（毫秒，默认 5000）

**返回值**: `Promise<void>`

**示例**:
```typescript
await page.click('button:has-text("保存")');
await waitForSuccessMessage(page);
```

---

### `waitForErrorMessage(page, timeout?)`

等待错误消息。

**参数**:
- `page: Page` - Playwright Page 对象
- `timeout?: number` - 超时时间（毫秒，默认 5000）

**返回值**: `Promise<void>`

**示例**:
```typescript
await page.click('button:has-text("提交")');
await waitForErrorMessage(page);
```

---

## 导航验证

### `verifyURL(page, expectedPattern)`

验证当前 URL（已存在的函数）。

**参数**:
- `page: Page` - Playwright Page 对象
- `expectedPattern: string | RegExp` - 期望的 URL 模式

**返回值**: `Promise<void>`

**示例**:
```typescript
await verifyURL(page, '/dashboard');
await verifyURL(page, /\/sessions\/[a-f0-9-]+/);
```

---

### `verifyRouteChange(page, fromRoute, toRoute)`

验证路由变化（已存在的函数）。

**参数**:
- `page: Page` - Playwright Page 对象
- `fromRoute: string` - 起始路由
- `toRoute: string` - 目标路由

**返回值**: `Promise<void>`

---

## 元素操作

### `waitForPageLoad(page)`

等待页面加载完成（已存在的函数）。

**参数**:
- `page: Page` - Playwright Page 对象

**返回值**: `Promise<void>`

---

### `waitForElement(page, selector, timeout?)`

等待元素可见（已存在的函数）。

**参数**:
- `page: Page` - Playwright Page 对象
- `selector: string` - 选择器
- `timeout?: number` - 超时时间（毫秒，默认 5000）

**返回值**: `Promise<void>`

---

### `waitForElementRemoved(page, selector, timeout?)`

等待元素被移除（已存在的函数）。

**参数**:
- `page: Page` - Playwright Page 对象
- `selector: string` - 选择器
- `timeout?: number` - 超时时间（毫秒，默认 5000）

**返回值**: `Promise<void>`

**示例**:
```typescript
await waitForElement(page, '.loading-spinner');
// ... 执行操作
await waitForElementRemoved(page, '.loading-spinner');
```

---

### `waitAndClick(page, selector, timeout?)`

等待元素可见并点击（已存在的函数）。

**参数**:
- `page: Page` - Playwright Page 对象
- `selector: string` - 选择器
- `timeout?: number` - 超时时间（毫秒，默认 5000）

**返回值**: `Promise<void>`

---

### `takeScreenshot(page, name)`

截图并保存（已存在的函数）。

**参数**:
- `page: Page` - Playwright Page 对象
- `name: string` - 截图名称（不含扩展名）

**返回值**: `Promise<void>`

**示例**:
```typescript
await takeScreenshot(page, 'test-scenario-1');
// 保存到 screenshots/test-scenario-1.png
```

---

### `uploadFile(page, selector, filePath)`

上传文件（已存在的函数）。

**参数**:
- `page: Page` - Playwright Page 对象
- `selector: string` - 文件输入框选择器
- `filePath: string` - 文件路径

**返回值**: `Promise<void>`

---

## 存储管理

### `getStorageState(page, key)`

获取存储状态（已存在的函数）。

**参数**:
- `page: Page` - Playwright Page 对象
- `key: string` - 键名

**返回值**: `Promise<string | null>`

**示例**:
```typescript
const token = await getStorageState(page, 'token');
```

---

### `setStorageState(page, state)`

设置存储状态（已存在的函数）。

**参数**:
- `page: Page` - Playwright Page 对象
- `state: Record<string, string>` - 状态对象

**返回值**: `Promise<void>`

**示例**:
```typescript
await setStorageState(page, {
  token: 'xxx',
  user: JSON.stringify({ name: 'test' })
});
```

---

## WebSocket

### `verifyWebSocketStatus(page, expectedStatus)`

验证 WebSocket 连接状态（已存在的函数）。

**参数**:
- `page: Page` - Playwright Page 对象
- `expectedStatus: 'connected' | 'connecting' | 'disconnected'` - 期望的状态

**返回值**: `Promise<void>`

---

### `waitForWebSocketMessage(page, timeout?)`

等待 WebSocket 消息（已存在的函数）。

**参数**:
- `page: Page` - Playwright Page 对象
- `timeout?: number` - 超时时间（毫秒，默认 5000）

**返回值**: `Promise<any>`

---

## 数据库验证

### `verifyDBRecord(page, endpoint, recordId)`

通过 API 验证数据库记录是否存在（已存在的函数）。

**参数**:
- `page: Page` - Playwright Page 对象
- `endpoint: string` - API 端点
- `recordId: string` - 记录 ID

**返回值**: `Promise<any>` - 记录数据

---

### `verifyRecordDeleted(page, endpoint, recordId)`

验证记录是否被删除（已存在的函数）。

**参数**:
- `page: Page` - Playwright Page 对象
- `endpoint: string` - API 端点
- `recordId: string` - 记录 ID

**返回值**: `Promise<void>`

---

### `getDBRecordCount(page, endpoint)`

获取数据库中的记录数量（已存在的函数）。

**参数**:
- `page: Page` - Playwright Page 对象
- `endpoint: string` - API 端点

**返回值**: `Promise<number>`

---

## 测试用户凭据

### `testUser`

默认测试用户凭据。

**类型**:
```typescript
{
  username: 'testuser';
  password: 'testpass123';
  email: 'test@example.com';
}
```

**示例**:
```typescript
import { testUser } from './helpers';

console.log(testUser.username);  // 'testuser'
console.log(testUser.password);  // 'testpass123'
console.log(testUser.email);     // 'test@example.com'
```

---

## 最佳实践

### 1. 优先使用快速登录

```typescript
// ✅ 推荐：快速登录，自动检查是否已登录
await quickLogin(page);

// ⚠️ 不推荐：每次都执行完整登录流程
await login(page);
```

### 2. 使用状态记录函数验证变化

```typescript
// ✅ 推荐：明确的状态变化验证
const before = await recordState(page, { count: '.item' });
await performAction(page);
const after = await recordState(page, { count: '.item' });
const diff = compareStates(before, after);

// ❌ 不推荐：不明确的变化验证
await performAction(page);
// 手动检查...
```

### 3. 监听网络请求

```typescript
// ✅ 推荐：监听请求确保操作完成
const request = waitForAPI(page, '/api/sessions', 'POST');
await page.click('button:has-text("创建")');
await request; // 等待请求完成

// ⚠️ 可能不可靠：仅依赖 UI 状态
await page.click('button:has-text("创建")');
await page.waitForTimeout(1000); // 魔法数字
```

### 4. 使用专用等待函数

```typescript
// ✅ 推荐：使用专用等待函数
await waitForSuccessMessage(page);
await waitForElementRemoved(page, '.loading');

// ⚠️ 可能不稳定：通用等待
await page.waitForTimeout(3000);
```

### 5. 清理测试数据

```typescript
test.afterEach(async ({ page }) => {
  // 清理状态
  await logoutAndCleanup(page);
});
```

---

## 相关文档

- [README.md](./README.md) - E2E 测试指南
- [helpers-examples.spec.ts](./helpers-examples.spec.ts) - 辅助函数使用示例
- [Playwright 官方文档](https://playwright.dev) - Playwright 官方文档
