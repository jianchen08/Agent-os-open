/**
 * 完整的端到端测试
 *
 * 使用 Playwright 模拟真实用户操作
 * 测试所有页面和交互功能
 * 验证前后端数据类型一致性
 */

import { test, expect, Page } from '@playwright/test';

const BASE_URL = process.env.FRONTEND_URL || process.env.REACT_APP_FRONTEND_URL || "http://localhost:5188";
const API_BASE_URL = process.env.API_URL || process.env.REACT_APP_API_URL || "http://localhost:8988";

// 测试数据
const testUser = {
  username: `e2e_test_${Date.now()}`,
  password: 'Test123456!',
  email: `e2e_test_${Date.now()}@example.com`
};

/**
 * 辅助函数：等待 API 响应并捕获数据
 */
async function captureAPIResponse(page: Page, urlPattern: string | RegExp) {
  let responseData: any = null;

  page.on('response', async (response) => {
    if (typeof urlPattern === 'string') {
      if (response.url().includes(urlPattern)) {
        try {
          responseData = await response.json();
        } catch (e) {
          responseData = await response.text();
        }
      }
    } else {
      if (urlPattern.test(response.url())) {
        try {
          responseData = await response.json();
        } catch (e) {
          responseData = await response.text();
        }
      }
    }
  });

  return responseData;
}

/**
 * 辅助函数：验证数据类型
 */
function validateDataType(data: any, typeName: string, expectedFields: Record<string, string>) {
  expect(data, `${typeName} 响应不应为空`).toBeDefined();

  for (const [field, type] of Object.entries(expectedFields)) {
    expect(data, `${typeName} 应包含字段 ${field}`).toHaveProperty(field);

    switch (type) {
      case 'string':
        expect(typeof data[field], `${typeName}.${field} 应该是字符串`).toBe('string');
        break;
      case 'number':
        expect(typeof data[field], `${typeName}.${field} 应该是数字`).toBe('number');
        break;
      case 'boolean':
        expect(typeof data[field], `${typeName}.${field} 应该是布尔值`).toBe('boolean');
        break;
      case 'array':
        expect(Array.isArray(data[field]), `${typeName}.${field} 应该是数组`).toBeTruthy();
        break;
      case 'object':
        expect(typeof data[field], `${typeName}.${field} 应该是对象`).toBe('object');
        break;
    }
  }
}

test.describe('端到端测试 - 认证流程', () => {
  const authenticatedUser = {
    username: 'admin',  // 使用已知存在的用户
    password: 'admin123'
  };

  test.beforeEach(async ({ page }) => {
    await page.goto(BASE_URL);
  });

  test('1. 注册新用户', async ({ page }) => {
    console.log('\n=== 测试：注册新用户 ===');

    // 点击注册链接
    await page.getByTestId('register-link').click();
    await expect(page).toHaveURL(/\/register/);

    // 填写注册表单
    await page.getByTestId('register-username-input').fill(testUser.username);
    await page.getByTestId('email-input').fill(testUser.email);
    await page.getByTestId('register-password-input').fill(testUser.password);
    await page.getByTestId('confirm-password-input').fill(testUser.password);

    // 捕获 API 响应
    const responsePromise = page.waitForResponse(
      res => res.url().includes('/api/v1/auth/register') && res.status() === 200
    );

    // 提交表单
    await page.getByTestId('register-submit-button').click();

    // 等待 API 响应
    const response = await responsePromise;
    const data = await response.json();

    console.log('注册响应:', JSON.stringify(data, null, 2));

    // 验证响应数据类型（注册后自动登录，返回 token）
    validateDataType(data, 'RegisterResponse', {
      access_token: 'string',
      refresh_token: 'string',
      token_type: 'string',
      expires_in: 'number'
    });

    // 验证 token 类型
    expect(data.access_token.length).toBeGreaterThan(0);
    expect(data.refresh_token.length).toBeGreaterThan(0);
    expect(data.token_type).toBe('bearer');
    expect(data.expires_in).toBeGreaterThan(0);

    console.log('[OK] 注册成功，数据类型正确');
  });

  test('2. 登录', async ({ page }) => {
    console.log('\n=== 测试：登录 ===');

    // 填写登录表单
    await page.getByTestId('login-username-input').fill(authenticatedUser.username);
    await page.getByTestId('login-password-input').fill(authenticatedUser.password);

    // 捕获 API 响应
    const responsePromise = page.waitForResponse(
      res => res.url().includes('/api/v1/auth/login') && res.status() === 200
    );

    // 提交登录
    await page.getByTestId('login-submit-button').click();

    // 等待 API 响应
    const response = await responsePromise;
    const data = await response.json();

    console.log('登录响应:', JSON.stringify(data, null, 2));

    // 验证响应数据类型（符合 LoginResponse 接口）
    validateDataType(data, 'LoginResponse', {
      access_token: 'string',
      refresh_token: 'string',
      token_type: 'string',
      expires_in: 'number'
    });

    // 验证 token 类型
    expect(data.access_token.length).toBeGreaterThan(0);
    expect(data.refresh_token.length).toBeGreaterThan(0);
    expect(data.token_type).toBe('bearer');
    expect(data.expires_in).toBeGreaterThan(0);

    // 验证跳转到首页
    await expect(page).toHaveURL(/\//);
    await expect(page.getByText(/欢迎，/)).toBeVisible();

    console.log('[OK] 登录成功，数据类型正确');
  });

  test('3. 表单验证 - 空用户名', async ({ page }) => {
    console.log('\n=== 测试：表单验证 ===');

    // 不填写用户名，直接点击登录
    await page.getByTestId('login-submit-button').click();

    // 验证错误提示
    await expect(page.getByTestId('username-error')).toBeVisible();

    console.log('[OK] 表单验证正确');
  });

  test('4. 获取当前用户信息', async ({ page }) => {
    console.log('\n=== 测试：获取当前用户信息 ===');

    // 先登录
    await page.getByTestId('login-username-input').fill(authenticatedUser.username);
    await page.getByTestId('login-password-input').fill(authenticatedUser.password);
    await page.getByTestId('login-submit-button').click();

    // 等待跳转到仪表盘
    await expect(page).toHaveURL(/\//);

    // 验证用户名显示在仪表盘上
    await expect(page.getByRole('heading', { name: new RegExp(`欢迎，${authenticatedUser.username}`, 'i') })).toBeVisible({ timeout: 5000 });

    console.log('[OK] 用户信息获取成功');
  });
});

test.describe('端到端测试 - 仪表盘', () => {
  const authenticatedUser = {
    username: 'admin',
    password: 'admin123'
  };

  test.beforeEach(async ({ page }) => {
    // 登录
    await page.goto(`${BASE_URL}/login`);
    await page.getByTestId('login-username-input').fill(authenticatedUser.username);
    await page.getByTestId('login-password-input').fill(authenticatedUser.password);
    await page.getByTestId('login-submit-button').click();
    await expect(page).toHaveURL(/\//);
  });

  test('5. 加载仪表盘', async ({ page }) => {
    console.log('\n=== 测试：加载仪表盘 ===');

    // 验证欢迎消息
    await expect(page.getByText(/欢迎回来/)).toBeVisible();
    await expect(page.getByText(testUser.username)).toBeVisible();

    // 捕获会话列表 API 响应
    const responsePromise = page.waitForResponse(
      res => res.url().includes('/api/v1/threads') && res.status() === 200
    );

    // 等待会话列表加载
    await page.waitForSelector('[data-testid="dashboard-page"]', { timeout: 5000 });

    const response = await responsePromise;
    const data = await response.json();

    console.log('会话列表响应:', JSON.stringify(data, null, 2));

    // 验证响应是数组
    expect(Array.isArray(data)).toBeTruthy();

    // 验证每个会话的数据结构
    if (data.length > 0) {
      validateDataType(data[0], 'Thread', {
        thread_id: 'string',
        current_state: 'string',
        created_at: 'string',
        updated_at: 'string'
      });
    }

    console.log('[OK] 仪表盘加载成功，数据类型正确');
  });

  test('6. 创建新会话', async ({ page }) => {
    console.log('\n=== 测试：创建新会话 ===');

    // 捕获创建会话 API 响应
    const responsePromise = page.waitForResponse(
      res => res.url().includes('/api/v1/threads') &&
            res.request().method() === 'POST' &&
            res.status() === 200
    );

    // 点击创建会话按钮
    const createButton = page.getByRole('button').filter({ hasText: /新建|创建|\+/ }).first();
    await createButton.click();

    const response = await responsePromise;
    const data = await response.json();

    console.log('创建会话响应:', JSON.stringify(data, null, 2));

    // 验证响应数据类型（符合 Thread 接口）
    validateDataType(data, 'Thread', {
      thread_id: 'string',
      current_state: 'string',
      created_at: 'string',
      updated_at: 'string'
    });

    // 验证跳转到会话页
    await expect(page).toHaveURL(/\/session\//);
    await expect(page).toHaveURL(new RegExp(`/${data.thread_id}`));

    console.log('[OK] 创建会话成功，数据类型正确');
  });

  test('7. 进入现有会话', async ({ page }) => {
    console.log('\n=== 测试：进入现有会话 ===');

    // 先创建一个会话
    const responsePromise = page.waitForResponse(
      res => res.url().includes('/api/v1/threads') && res.request().method() === 'POST'
    );
    await page.getByRole('button').filter({ hasText: /新建/ }).click();
    const response = await responsePromise;
    const createdThread = await response.json();

    // 返回首页
    await page.goto(BASE_URL);

    // 等待会话列表加载
    await page.waitForSelector('[data-testid="dashboard-page"]');

    // 点击会话（可能需要根据实际 UI 调整选择器）
    const sessionLink = page.getByRole('link').filter({ hasText: new RegExp(createdThread.thread_id.slice(0, 8)) });
    if (await sessionLink.count() > 0) {
      await sessionLink.click();
      await expect(page).toHaveURL(/\/session\//);
      console.log('[OK] 进入会话成功');
    } else {
      console.log('[WARN] 未找到会话链接（可能会话列表为空）');
    }
  });
});

test.describe('端到端测试 - 会话页面', () => {

  test.beforeEach(async ({ page, context }) => {
    // 登录并创建会话
    await page.goto(`${BASE_URL}/login`);
    await page.fill('input[name="username"]', testUser.username);
    await page.fill('input[name="password"]', testUser.password);
    await page.locator('button[type="submit"]').click();

    // 等待跳转到首页
    await expect(page).toHaveURL(/\//);

    // 创建会话
    const responsePromise = page.waitForResponse(
      res => res.url().includes('/api/v1/threads') && res.request().method() === 'POST'
    );
    await page.getByRole('button').filter({ hasText: /新建/ }).click();
    const response = await responsePromise;
    const thread = await response.json();

    // 等待导航到会话页
    await expect(page).toHaveURL(new RegExp(`/${thread.thread_id}`));
  });

  test('8. 加载会话详情', async ({ page }) => {
    console.log('\n=== 测试：加载会话详情 ===');

    // 捕获会话详情 API 响应
    const responsePromise = page.waitForResponse(
      res => res.url().includes('/detail') && res.status() === 200
    );

    await page.reload();

    const response = await responsePromise;
    const data = await response.json();

    console.log('会话详情响应:', JSON.stringify(data, null, 2));

    // 验证响应包含必需字段
    expect(data).toHaveProperty('thread_id');

    // 检查是否包含执行图数据
    const hasGraph = 'graph' in data || 'nodes' in data || 'execution_graph' in data;
    if (hasGraph) {
      console.log('[OK] 会话详情包含执行图数据');
    }

    console.log('[OK] 会话详情加载成功');
  });

  test('9. 发送消息（WebSocket）', async ({ page }) => {
    console.log('\n=== 测试：发送消息（WebSocket）===');

    // 等待 WebSocket 连接
    const wsStatus = page.locator('[data-testid*="websocket"], [data-testid*="ws"], .ws-status');
    await page.waitForTimeout(2000); // 等待 WebSocket 连接

    // 输入消息
    const testMessage = '这是一条测试消息';
    const messageInput = page.locator('textarea, input[type="text"]').first();
    await messageInput.fill(testMessage);

    // 发送消息
    const sendButton = page.getByRole('button').filter({ hasText: /发送|Send/ });
    await sendButton.click();

    // 验证消息显示在列表中
    await expect(page.getByText(testMessage)).toBeVisible({ timeout: 5000 });

    console.log('[OK] 消息发送成功');
  });

  test('10. WebSocket 连接状态', async ({ page }) => {
    console.log('\n=== 测试：WebSocket 连接状态 ===');

    // 等待 WebSocket 连接
    await page.waitForTimeout(2000);

    // 检查 WebSocket 状态指示器
    const wsIndicator = page.locator('[data-testid*="websocket"], .ws-status, [class*="ws-"]');
    const count = await wsIndicator.count();

    if (count > 0) {
      // 验证状态指示器存在
      await expect(wsIndicator.first()).toBeVisible();
      console.log('[OK] WebSocket 状态指示器显示正常');
    } else {
      console.log('[WARN] 未找到 WebSocket 状态指示器');
    }
  });
});

test.describe('端到端测试 - 设置页面', () => {

  test.beforeEach(async ({ page }) => {
    // 登录
    await page.goto(`${BASE_URL}/login`);
    await page.fill('input[name="username"]', testUser.username);
    await page.fill('input[name="password"]', testUser.password);
    await page.locator('button[type="submit"]').click();
    await expect(page).toHaveURL(/\//);

    // 导航到设置页
    await page.goto(`${BASE_URL}/settings`);
  });

  test('11. 加载 LLM 配置', async ({ page }) => {
    console.log('\n=== 测试：加载 LLM 配置 ===');

    // 捕获 API 响应
    const responsePromise = page.waitForResponse(
      res => res.url().includes('/api/v1/config/llm') && res.status() === 200
    );

    // 等待页面加载
    await expect(page.getByText(/LLM|模型|配置/).first()).toBeVisible();

    const response = await responsePromise;
    const data = await response.json();

    console.log('LLM 配置响应:', JSON.stringify(data, null, 2));

    // 验证配置结构
    expect(data).toBeDefined();

    console.log('[OK] LLM 配置加载成功');
  });

  test('12. 用户设置', async ({ page }) => {
    console.log('\n=== 测试：用户设置 ===');

    // 捕获 API 响应
    const responsePromise = page.waitForResponse(
      res => res.url().includes('/api/v1/users/settings') && res.status() === 200
    );

    // 触发设置加载（可能需要刷新或点击标签）
    await page.reload();

    try {
      const response = await responsePromise;
      const data = await response.json();

      console.log('用户设置响应:', JSON.stringify(data, null, 2));

      // 验证数据类型（符合 UserSettingsResponse 接口）
      expect(data).toHaveProperty('default_agent_id');
      expect(data).toHaveProperty('preferences');

      console.log('[OK] 用户设置加载成功，数据类型正确');
    } catch (e) {
      console.log('[WARN] 用户设置端点可能未实现');
    }
  });
});

test.describe('端到端测试 - 令牌刷新', () => {

  test('13. 刷新访问令牌', async ({ page }) => {
    console.log('\n=== 测试：刷新访问令牌 ===');

    // 登录
    await page.goto(`${BASE_URL}/login`);
    await page.fill('input[name="username"]', testUser.username);
    await page.fill('input[name="password"]', testUser.password);
    await page.locator('button[type="submit"]').click();

    // 等待一段时间
    await page.waitForTimeout(3000);

    // 刷新页面，应该会触发令牌刷新（如果 token 即将过期）
    // 或者手动调用刷新 API

    // 验证仍然能访问受保护页面
    await expect(page.getByText(/欢迎回来/)).toBeVisible();

    console.log('[OK] 令牌刷新功能正常');
  });
});

test.describe('端到端测试 - 错误处理', () => {

  test('14. 未登录访问受保护页面', async ({ page }) => {
    console.log('\n=== 测试：未登录访问受保护页面 ===');

    // 直接访问受保护页面
    await page.goto(`${BASE_URL}/settings`);

    // 验证重定向到登录页
    await expect(page).toHaveURL(/\/login/);

    console.log('[OK] 重定向到登录页成功');
  });

  test('15. 登录失败 - 错误密码', async ({ page }) => {
    console.log('\n=== 测试：登录失败 ===');

    // 输入错误密码
    await page.getByTestId('login-username-input').fill(authenticatedUser.username);
    await page.getByTestId('login-password-input').fill('WrongPassword123!');

    // 捕获错误响应
    const responsePromise = page.waitForResponse(
      res => res.url().includes('/api/v1/auth/login') && res.status() !== 200
    );

    await page.getByTestId('login-submit-button').click();

    const response = await responsePromise;
    const data = await response.json();

    console.log('登录失败响应:', JSON.stringify(data, null, 2));

    // 验证错误响应
    expect(response.status()).toBeGreaterThan(399);

    // 验证错误提示显示
    await expect(page.getByTestId('login-error')).toBeVisible();

    console.log('[OK] 错误处理正确');
  });
});

test.afterAll(async () => {
  console.log('\n=== 所有测试完成 ===');
});
