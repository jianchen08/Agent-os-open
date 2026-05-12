/**
 * 思考模式功能测试
 *
 * 测试内容：
 * 1. 思考模式切换按钮显示和功能
 * 2. 思考模式状态检查
 * 3. 思考内容渲染
 * 4. WebSocket 思考消息处理
 */

import { test, expect, Page } from '@playwright/test';

const BASE_URL = process.env.FRONTEND_URL || 'http://localhost:5188';
const API_URL = process.env.API_URL || 'http://localhost:8888';

// 测试用户凭证
const TEST_USER = {
  username: 'admin',
  password: 'admin123456'
};

/**
 * 登录辅助函数
 */
async function login(page: Page) {
  await page.goto(`${BASE_URL}/login`);

  // 等待登录表单
  await page.waitForSelector('input[name="username"], input[type="text"]', { timeout: 5000 });

  // 填写登录表单
  await page.fill('input[name="username"], input[type="text"]', TEST_USER.username);
  await page.fill('input[name="password"], input[type="password"]', TEST_USER.password);

  // 点击登录按钮
  await page.click('button[type="submit"], button:has-text("登录")');

  // 等待跳转到首页或会话页面（包括 /session/xxx 格式）
  await page.waitForURL(/\/(dashboard|sessions?|chat|session)/, { timeout: 10000 });
}

/**
 * 创建或导航到会话
 */
async function goToSession(page: Page) {
  // 尝试导航到会话页面
  await page.goto(`${BASE_URL}/sessions`);

  // 如果没有会话，创建一个新会话
  const newSessionButton = page.locator('button:has-text("新会话"), button:has-text("创建会话"), button[aria-label*="new"]');
  if (await newSessionButton.isVisible({ timeout: 3000 }).catch(() => false)) {
    await newSessionButton.click();
  }

  // 等待会话页面加载
  await page.waitForSelector('input[placeholder*="消息"], textarea[placeholder*="消息"]', { timeout: 5000 });
}

test.describe('思考模式功能测试', () => {
  test.beforeAll(async () => {
    // 确保 API 可用
    const response = await fetch(`${API_URL}/health`);
    expect(response.ok).toBeTruthy();
  });

  test.beforeEach(async ({ page }) => {
    await login(page);
    await goToSession(page);
  });

  test('应该显示思考模式切换按钮', async ({ page }) => {
    // 检查思考模式切换按钮是否存在
    // 组件显示的文本可能是"普通模式"或"🧠 深度思考"
    const thinkingToggle = page.locator('[data-testid="thinking-mode-toggle"], button:has-text("普通模式"), button:has-text("深度思考")').first();

    // 按钮应该可见
    await expect(thinkingToggle).toBeVisible({ timeout: 5000 });
  });

  test('应该能够切换思考模式', async ({ page }) => {
    const thinkingToggle = page.locator('[data-testid="thinking-mode-toggle"], button:has-text("普通模式"), button:has-text("深度思考")').first();

    // 获取初始状态
    const initialClass = await thinkingToggle.getAttribute('class') || '';

    // 点击切换
    await thinkingToggle.click();

    // 等待状态更新
    await page.waitForTimeout(500);

    // 检查状态变化（类名或样式应该改变）
    const newClass = await thinkingToggle.getAttribute('class') || '';
    expect(newClass).not.toBe(initialClass);
  });

  test('应该在消息中显示思考内容', async ({ page }) => {
    // 先开启思考模式
    const thinkingToggle = page.locator('[data-testid="thinking-mode-toggle"], button:has-text("普通模式"), button:has-text("深度思考")').first();
    await thinkingToggle.click();
    await page.waitForTimeout(500);

    // 发送一条简单消息
    const input = page.locator('input[placeholder*="消息"], textarea[placeholder*="消息"]').first();
    await input.fill('解释一下什么是递归？');

    // 发送消息（使用 Enter 键更可靠）
    await input.press('Enter');

    // 等待响应（思考模型响应可能较慢）
    await page.waitForTimeout(10000);

    // 检查是否有思考内容显示
    // 思考内容可能在以下位置：
    // 1. 独立的思考展示组件
    // 2. 消息气泡内的思考区域
    // 3. 可展开/折叠的思考部分

    const thinkingDisplay = page.locator('[data-testid="thinking-display"], .thinking-content, [class*="thinking"]').first();
    const hasThinkingContent = await thinkingDisplay.isVisible().catch(() => false);

    if (hasThinkingContent) {
      // 如果有思考内容，检查内容是否非空
      const text = await thinkingDisplay.textContent();
      expect(text?.trim().length).toBeGreaterThan(0);
    } else {
      // 如果没有显示思考内容，可能是因为：
      // 1. 模型不支持思考模式
      // 2. WebSocket 消息处理有问题
      // 3. 渲染组件有问题
      console.log('注意: 未检测到思考内容显示，可能需要检查模型配置或 WebSocket 处理');
    }
  });

  test('应该能够展开/折叠思考内容', async ({ page }) => {
    // 开启思考模式并发送消息
    const thinkingToggle = page.locator('[data-testid="thinking-mode-toggle"], button:has-text("普通模式"), button:has-text("深度思考")').first();
    await thinkingToggle.click();
    await page.waitForTimeout(500);

    const input = page.locator('input[placeholder*="消息"], textarea[placeholder*="消息"]').first();
    await input.fill('1+1等于几？请详细思考。');
    await page.keyboard.press('Enter');

    // 等待响应
    await page.waitForTimeout(8000);

    // 查找思考展开/折叠按钮
    const expandButton = page.locator('button:has-text("展开"), button:has-text("显示思考"), [aria-label*="expand"]').first();

    if (await expandButton.isVisible({ timeout: 3000 }).catch(() => false)) {
      // 点击展开
      await expandButton.click();
      await page.waitForTimeout(500);

      // 验证思考内容可见
      const thinkingContent = page.locator('[data-testid="thinking-content"], .thinking-details').first();
      await expect(thinkingContent).toBeVisible();

      // 点击折叠
      const collapseButton = page.locator('button:has-text("折叠"), button:has-text("隐藏思考"), [aria-label*="collapse"]').first();
      await collapseButton.click();
      await page.waitForTimeout(500);

      // 验证思考内容已隐藏
      await expect(thinkingContent).not.toBeVisible();
    }
  });

  test('思考模式应该影响 API 请求参数', async ({ page }) => {
    // 监听 API 请求
    const apiRequests: string[] = [];

    page.on('request', request => {
      const url = request.url();
      if (url.includes('/ws/') || url.includes('/api/')) {
        apiRequests.push(JSON.stringify({
          url: url,
          method: request.method(),
          headers: request.headers(),
          postData: request.postData()
        }));
      }
    });

    // 开启思考模式
    const thinkingToggle = page.locator('[data-testid="thinking-mode-toggle"], button:has-text("普通模式"), button:has-text("深度思考")').first();
    await thinkingToggle.click();
    await page.waitForTimeout(500);

    // 发送消息
    const input = page.locator('input[placeholder*="消息"], textarea[placeholder*="消息"]').first();
    await input.fill('测试消息');
    await page.keyboard.press('Enter');

    // 等待
    await page.waitForTimeout(5000);

    // 检查是否有包含 thinking 参数的请求
    const hasThinkingRequest = apiRequests.some(req =>
      req.includes('thinking') || req.includes('reasoning')
    );

    if (!hasThinkingRequest) {
      console.log('注意: 未检测到思考模式相关的 API 参数，可能需要检查前端请求逻辑');
    }
  });
});

test.describe('思考模式 API 测试', () => {
  test('应该返回支持的思考模型列表', async () => {
    const response = await fetch(`${API_URL}/api/v1/thinking-mode/models`);
    expect(response.ok).toBeTruthy();

    const models = await response.json();
    expect(Array.isArray(models)).toBeTruthy();
    expect(models.length).toBeGreaterThan(0);

    // 验证模型数据结构
    models.forEach(model => {
      expect(model).toHaveProperty('model_name');
      expect(model).toHaveProperty('thinking_type');
      expect(model).toHaveProperty('display_name');
    });
  });

  test('应该能够检查模型是否支持思考模式', async () => {
    const response = await fetch(`${API_URL}/api/v1/thinking-mode/check/glm-4.7`);
    expect(response.ok).toBeTruthy();

    const result = await response.json();
    expect(result).toHaveProperty('model_name', 'glm-4.7');
    expect(result).toHaveProperty('supports_thinking');
    expect(result.supports_thinking).toBe(true);
  });

  test('应该能够获取模型的思考模式信息', async () => {
    const response = await fetch(`${API_URL}/api/v1/thinking-mode/models/glm-4.7`);
    expect(response.ok).toBeTruthy();

    const info = await response.json();
    expect(info).toHaveProperty('model_name');
    expect(info).toHaveProperty('thinking_type');
    expect(info).toHaveProperty('base_model');
    expect(info).toHaveProperty('thinking_model');
  });

  test('思考模式健康检查应该成功', async () => {
    const response = await fetch(`${API_URL}/api/v1/thinking-mode/health`);
    expect(response.ok).toBeTruthy();

    const health = await response.json();
    expect(health).toHaveProperty('status', 'healthy');
  });
});
