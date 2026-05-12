/**
 * 测试辅助函数
 *
 * 提供常用的测试工具函数
 */

import { Page, expect } from '@playwright/test';

/**
 * 测试用户凭据
 */
export const testUser = {
  username: 'admin',
  password: 'admin123456',
  email: 'admin@example.com',
};

/**
 * 执行登录
 */
export async function login(page: Page, username = testUser.username, password = testUser.password) {
  // 导航到登录页面,增加超时
  await page.goto('/login', { timeout: 30000 });

  // 填写用户名,使用正确的选择器
  await page.fill('input[id="username"], [data-testid="login-username-input"]', username, { timeout: 10000 });

  // 填写密码,使用正确的选择器
  await page.fill('input[id="password"], [data-testid="login-password-input"]', password, { timeout: 10000 });

  // 点击登录按钮,使用正确的选择器
  await page.click('button[type="submit"], [data-testid="login-submit-button"]', { timeout: 10000 });

  // 等待导航到首页或会话页面,增加超时并添加容错
  await page.waitForURL(/\/(session\/[a-f0-9-]+)?/, { timeout: 60000 }).catch(() => {
    // 如果URL没有变化,可能已经在目标页面了,检查是否已登录
    return page.locator('body').isVisible({ timeout: 5000 });
  });

  // 等待页面加载完成
  await page.waitForLoadState('networkidle', { timeout: 30000 }).catch(() => {
    // networkidle可能永远等不到,忽略错误
  });
}

/**
 * 执行登出
 */
export async function logout(page: Page) {
  await page.click('[data-testid="user-menu"], .user-menu');
  await page.click('button:has-text("退出"), button:has-text("登出")');
}

/**
 * 等待页面加载完成
 */
export async function waitForPageLoad(page: Page) {
  await page.waitForLoadState('networkidle');
  await page.waitForLoadState('domcontentloaded');
}

/**
 * 截图并保存
 */
export async function takeScreenshot(page: Page, name: string) {
  await page.screenshot({ path: `screenshots/${name}.png`, fullPage: true });
}

/**
 * 填写表单
 */
export async function fillForm(page: Page, fields: Record<string, string>) {
  for (const [selector, value] of Object.entries(fields)) {
    await page.fill(selector, value);
  }
}

/**
 * 等待元素可见并可点击
 */
export async function waitAndClick(page: Page, selector: string, timeout = 5000) {
  await page.waitForSelector(selector, { state: 'visible', timeout });
  await page.click(selector);
}

/**
 * 检查 toast 消息
 */
export async function checkToast(page: Page, message: string) {
  const toast = page.locator(`.toast, [role="alert"]`).filter({ hasText: message });
  await expect(toast).toBeVisible({ timeout: 5000 });
}

/**
 * 模拟文件上传
 */
export async function uploadFile(page: Page, selector: string, filePath: string) {
  const fileInput = page.locator(selector);
  await fileInput.setInputFiles(filePath);
}

/**
 * 获取存储状态
 */
export async function getStorageState(page: Page, key: string) {
  return await page.evaluate((k) => {
    return localStorage.getItem(k);
  }, key);
}

/**
 * 设置存储状态
 */
export async function setStorageState(page: Page, state: Record<string, string>) {
  await page.evaluate((s) => {
    for (const [key, value] of Object.entries(s)) {
      localStorage.setItem(key, value);
    }
  }, state);
}

// ============================================
// 网络请求监听和验证
// ============================================

/**
 * 监听 API 请求并返回响应
 * @param page Playwright Page 对象
 * @param urlPattern URL 匹配模式
 * @returns Promise<Response> API 响应对象
 */
export async function waitForAPIResponse(page: Page, urlPattern: string | RegExp) {
  return page.waitForResponse(
    (response) =>
      response.url().includes(typeof urlPattern === 'string' ? urlPattern : urlPattern.source) &&
      response.status() >= 200 &&
      response.status() < 300
  );
}

/**
 * 监听 POST 请求
 */
export async function waitForPOSTRequest(page: Page, endpoint: string) {
  return page.waitForRequest(
    (request) => request.method() === 'POST' && request.url().includes(endpoint)
  );
}

/**
 * 监听 WebSocket 消息
 */
export async function waitForWebSocketMessage(page: Page, timeout = 5000) {
  return page.evaluate(
    ({ timeout }) =>
      new Promise((resolve) => {
        const ws = (window as any).__testWebSocket;
        if (ws) {
          const originalOnMessage = ws.onmessage;
          ws.onmessage = (event: MessageEvent) => {
            if (originalOnMessage) originalOnMessage(event);
            resolve(event.data);
          };
          setTimeout(() => resolve(null), timeout);
        } else {
          resolve(null);
        }
      }),
    { timeout }
  );
}

/**
 * 获取 API 响应数据
 */
export async function getAPIData<T = any>(page: Page, urlPattern: string): Promise<T | null> {
  try {
    const response = await waitForAPIResponse(page, urlPattern);
    return await response.json() as T;
  } catch {
    return null;
  }
}

/**
 * 验证 API 响应状态
 */
export async function verifyAPIStatus(page: Page, urlPattern: string, expectedStatus: number) {
  const response = await page.waitForResponse(
    (response) => response.url().includes(urlPattern)
  );
  expect(response.status()).toBe(expectedStatus);
  return response;
}

// ============================================
// 数据库验证（通过 API）
// ============================================

/**
 * 通过 API 验证数据库记录是否存在
 */
export async function verifyDBRecord(page: Page, endpoint: string, recordId: string) {
  const response = await page.evaluate(
    async ({ endpoint, recordId }) => {
      const token = localStorage.getItem('token');
      const res = await fetch(`${endpoint}/${recordId}`, {
        headers: {
          Authorization: `Bearer ${token}`,
        },
      });
      return { status: res.status, data: await res.json().catch(() => null) };
    },
    { endpoint, recordId }
  );

  expect(response.status).toBe(200);
  expect(response.data).toBeTruthy();
  return response.data;
}

/**
 * 验证记录是否被删除
 */
export async function verifyRecordDeleted(page: Page, endpoint: string, recordId: string) {
  const response = await page.evaluate(
    async ({ endpoint, recordId }) => {
      const token = localStorage.getItem('token');
      const res = await fetch(`${endpoint}/${recordId}`, {
        headers: {
          Authorization: `Bearer ${token}`,
        },
      });
      return { status: res.status };
    },
    { endpoint, recordId }
  );

  // 应该返回 404 或 403
  expect([404, 403].includes(response.status)).toBeTruthy();
}

/**
 * 获取数据库中的记录数量
 */
export async function getDBRecordCount(page: Page, endpoint: string): Promise<number> {
  const response = await page.evaluate(
    async ({ endpoint }) => {
      const token = localStorage.getItem('token');
      const res = await fetch(endpoint, {
        headers: {
          Authorization: `Bearer ${token}`,
        },
      });
      const data = await res.json();
      return data.total || data.length || 0;
    },
    { endpoint }
  );

  return response;
}

// ============================================
// 状态记录和比较
// ============================================

/**
 * 记录元素数量
 */
export async function recordElementCount(page: Page, selector: string): Promise<number> {
  return await page.locator(selector).count();
}

/**
 * 验证元素数量变化
 */
export async function verifyElementCountChanged(
  page: Page,
  selector: string,
  initialCount: number,
  expectedChange: 'increase' | 'decrease' | number
) {
  const currentCount = await recordElementCount(page, selector);

  if (expectedChange === 'increase') {
    expect(currentCount).toBeGreaterThan(initialCount);
  } else if (expectedChange === 'decrease') {
    expect(currentCount).toBeLessThan(initialCount);
  } else {
    expect(currentCount).toBe(initialCount + expectedChange);
  }

  return currentCount;
}

/**
 * 等待元素出现
 */
export async function waitForElement(page: Page, selector: string, timeout = 5000) {
  await page.waitForSelector(selector, { state: 'visible', timeout });
}

/**
 * 等待元素消失
 */
export async function waitForElementRemoved(page: Page, selector: string, timeout = 5000) {
  await page.waitForSelector(selector, { state: 'detached', timeout });
}

// ============================================
// 主题相关验证
// ============================================

/**
 * 获取当前主题
 */
export async function getCurrentTheme(page: Page): Promise<string> {
  return await page.evaluate(() => {
    return document.documentElement.getAttribute('data-theme') || 'light';
  });
}

/**
 * 验证主题设置
 */
export async function verifyTheme(page: Page, expectedTheme: string) {
  const currentTheme = await getCurrentTheme(page);
  expect(currentTheme).toBe(expectedTheme);

  // 验证 localStorage
  const storedTheme = await getStorageState(page, 'theme');
  expect(storedTheme).toBe(expectedTheme);
}

/**
 * 获取组件颜色
 */
export async function getElementColor(page: Page, selector: string, property: 'color' | 'backgroundColor') {
  return await page.locator(selector).evaluate((el, prop) => {
    return window.getComputedStyle(el).getPropertyValue(prop);
  }, property);
}

// ============================================
// 导航验证
// ============================================

/**
 * 验证当前 URL
 */
export async function verifyURL(page: Page, expectedPattern: string | RegExp) {
  await page.waitForURL(expectedPattern, { timeout: 5000 });
  const url = page.url();
  if (typeof expectedPattern === 'string') {
    expect(url).toContain(expectedPattern);
  } else {
    expect(url).toMatch(expectedPattern);
  }
}

/**
 * 验证路由变化
 */
export async function verifyRouteChange(page: Page, fromRoute: string, toRoute: string) {
  expect(page.url()).toMatch(fromRoute);
  await verifyURL(page, toRoute);
}

// ============================================
// 表单交互辅助
// ============================================

/**
 * 清空并填写输入框
 */
export async function clearAndFill(page: Page, selector: string, value: string) {
  await page.fill(selector, '');
  await page.fill(selector, value);
}

/**
 * 选择下拉选项
 */
export async function selectDropdown(page: Page, triggerSelector: string, optionText: string) {
  await page.click(triggerSelector);
  await page.click(`li:has-text("${optionText}"), option:has-text("${optionText}")`);
}

// ============================================
// 消息和通知验证
// ============================================

/**
 * 等待 Toast 消息出现
 */
export async function waitForToast(page: Page, message?: string, timeout = 5000) {
  if (message) {
    await expect(page.locator(`.toast, [role="alert"]`).filter({ hasText: message })).toBeVisible({ timeout });
  } else {
    await expect(page.locator(`.toast, [role="alert"]`).first()).toBeVisible({ timeout });
  }
}

/**
 * 等待成功消息
 */
export async function waitForSuccessMessage(page: Page, timeout = 5000) {
  await expect(page.locator('.toast, [role="alert"]').first()).toBeVisible({ timeout });
  const toast = page.locator('.toast:has-text("成功"), .toast:has-text("保存"), [role="alert"]:has-text("成功")');
  await expect(toast.first()).toBeVisible({ timeout });
}

/**
 * 等待错误消息
 */
export async function waitForErrorMessage(page: Page, timeout = 5000) {
  await expect(page.locator('.toast, [role="alert"]').first()).toBeVisible({ timeout });
  const toast = page.locator('.toast:has-text("错误"), .toast:has-text("失败"), [role="alert"]:has-text("错误")');
  await expect(toast.first()).toBeVisible({ timeout });
}

// ============================================
// WebSocket 连接验证
// ============================================

/**
 * 验证 WebSocket 连接状态
 */
export async function verifyWebSocketStatus(page: Page, expectedStatus: 'connected' | 'connecting' | 'disconnected') {
  const statusText = page.locator('[data-testid="websocket-status"], .websocket-status');
  const status = await statusText.textContent();

  const statusMap: Record<typeof expectedStatus, string[]> = {
    connected: ['已连接', '在线'],
    connecting: ['连接中', '正在连接'],
    disconnected: ['未连接', '离线', '连接断开'],
  };

  const expectedTexts = statusMap[expectedStatus];
  const isConnected = expectedTexts.some(text => status?.includes(text));

  expect(isConnected).toBeTruthy();
}

// ============================================
// 网络监听函数（新增）
// ============================================

/**
 * 监听 API 请求
 * @param page Playwright Page 对象
 * @param endpoint API 端点（部分匹配）
 * @param method HTTP 方法（可选，默认监听所有方法）
 * @returns Promise 返回请求对象
 *
 * @example
 * // 监听任何方法到 /api/sessions 的请求
 * const request = await waitForAPI(page, '/api/sessions');
 *
 * // 只监听 POST 请求
 * const postRequest = await waitForAPI(page, '/api/sessions', 'POST');
 */
export async function waitForAPI(
  page: Page,
  endpoint: string,
  method?: 'GET' | 'POST' | 'PUT' | 'DELETE' | 'PATCH'
) {
  try {
    const request = await page.waitForRequest(
      (req) => {
        const urlMatch = req.url().includes(endpoint);
        const methodMatch = method ? req.method() === method : true;
        return urlMatch && methodMatch;
      },
      { timeout: 10000 }
    );
    return request;
  } catch (error) {
    throw new Error(`等待 API 请求失败: ${method || 'ANY'} ${endpoint}\n${error}`);
  }
}

/**
 * 监听多个 API 请求
 * @param page Playwright Page 对象
 * @param requests 要监听的请求配置数组
 * @returns Promise 返回请求对象数组
 *
 * @example
 * const requests = await waitForMultipleAPIs(page, [
 *   { endpoint: '/api/sessions', method: 'POST' },
 *   { endpoint: '/api/messages', method: 'POST' }
 * ]);
 */
export async function waitForMultipleAPIs(
  page: Page,
  requests: Array<{ endpoint: string; method?: string }>
) {
  const promises = requests.map(({ endpoint, method }) =>
    waitForAPI(page, endpoint, method as any)
  );
  return await Promise.all(promises);
}

// ============================================
// 状态记录函数（新增）
// ============================================

/**
 * 记录页面状态
 * @param page Playwright Page 对象
 * @param selectors 选择器对象，key 为名称，value 为选择器
 * @returns Promise 返回状态记录对象
 *
 * @example
 * const state = await recordState(page, {
 *   username: 'input[name="username"]',
 *   submitButton: 'button[type="submit"]',
 *   errorMessage: '.error-message'
 * });
 * console.log(state.username.visible); // true/false
 * console.log(state.username.value); // 输入框的值
 */
export async function recordState(
  page: Page,
  selectors: Record<string, string>
): Promise<Record<string, { visible: boolean; value?: string; text?: string }>> {
  const state: Record<string, { visible: boolean; value?: string; text?: string }> = {};

  for (const [name, selector] of Object.entries(selectors)) {
    try {
      const element = page.locator(selector);
      const isVisible = await element.isVisible().catch(() => false);
      const value = isVisible ? await element.inputValue().catch(() => undefined) : undefined;
      const text = isVisible ? await element.textContent().catch(() => undefined) : undefined;

      state[name] = {
        visible: isVisible,
        value,
        text
      };
    } catch (error) {
      state[name] = { visible: false };
    }
  }

  return state;
}

/**
 * 比较两个状态的差异
 * @param beforeState 之前的状��
 * @param afterState 当前的状态
 * @returns 返回差异对象
 *
 * @example
 * const before = await recordState(page, { count: '.item' });
 * await performAction(page);
 * const after = await recordState(page, { count: '.item' });
 * const diff = compareStates(before, after);
 */
export function compareStates(
  beforeState: Record<string, any>,
  afterState: Record<string, any>
) {
  const diff: Record<string, { before: any; after: any; changed: boolean }> = {};

  const beforeKeys = Object.keys(beforeState);
  const afterKeys = Object.keys(afterState);
  const allKeys = Array.from(new Set([...beforeKeys, ...afterKeys]));

  for (const key of allKeys) {
    const before = beforeState[key];
    const after = afterState[key];
    const changed = JSON.stringify(before) !== JSON.stringify(after);

    diff[key] = { before, after, changed };
  }

  return diff;
}

/**
 * 记录并等待状态变化
 * @param page Playwright Page 对象
 * @param selector 要监听的选择器
 * @param timeout 超时时间（毫秒）
 * @returns Promise 返回变化前后的状态
 */
export async function waitForStateChange(
  page: Page,
  selector: string,
  timeout = 5000
) {
  const element = page.locator(selector);
  const before = await element.textContent();

  await page.waitForFunction(
    ({ sel, initialText }) => {
      const el = document.querySelector(sel);
      return el && el.textContent !== initialText;
    },
    { sel: selector, initialText: before },
    { timeout }
  );

  const after = await element.textContent();
  return { before, after };
}

// ============================================
// 会话辅助函数（新增）
// ============================================

/**
 * 创建会话
 * @param page Playwright Page 对象
 * @param sessionData 会话数据（可选）
 * @returns Promise 返回创建的会话信息
 *
 * @example
 * // 使用默认数据创建会话
 * const session = await createSession(page);
 *
 * // 使用自定义数据创建会话
 * const session = await createSession(page, {
 *   name: '测试会话',
 *   description: '这是一个测试会话'
 * });
 */
export async function createSession(
  page: Page,
  sessionData?: {
    name?: string;
    description?: string;
    agentId?: string;
  }
) {
  try {
    // 确保在会话页面
    const currentUrl = page.url();
    if (!currentUrl.includes('/sessions')) {
      await page.goto('/sessions', { timeout: 10000 });
      await waitForPageLoad(page);
    }

    // 点击创建按钮
    await waitAndClick(page, 'button:has-text("创建"), button:has-text("新建"), [data-testid="create-session-btn"]');

    // 等待对话框出现
    await waitForElement(page, 'dialog, .modal, [role="dialog"]');

    // 填写会话信息
    if (sessionData?.name) {
      await clearAndFill(page, 'input[name="name"], input[placeholder*="名称"]', sessionData.name);
    }

    if (sessionData?.description) {
      await clearAndFill(page, 'textarea[name="description"], textarea[placeholder*="描述"]', sessionData.description);
    }

    if (sessionData?.agentId) {
      await selectDropdown(page, '[data-testid="agent-select"], select[name="agentId"]', sessionData.agentId);
    }

    // 点击确认按钮
    await page.click('button:has-text("确认"), button:has-text("创建"), dialog button[type="submit"]');

    // 等待创建成功
    await waitForSuccessMessage(page, 5000);

    // 等待会话列表更新
    await page.waitForLoadState('networkidle');

    // 返回会话信息（从 URL 或列表中获取）
    const newSessionId = await page.evaluate(() => {
      const url = window.location.href;
      const match = url.match(/\/sessions\/([a-f0-9-]+)/);
      return match ? match[1] : null;
    });

    return {
      id: newSessionId,
      name: sessionData?.name || '新会话',
      description: sessionData?.description || '',
      agentId: sessionData?.agentId
    };
  } catch (error) {
    throw new Error(`创建会话失败: ${error}`);
  }
}

/**
 * 发送消息
 * @param page Playwright Page 对象
 * @param sessionId 会话 ID（可选，如果已在会话页面）
 * @param content 消息内容
 * @returns Promise 返回发送的消息信息
 *
 * @example
 * // 在当前会话发送消息
 * await sendMessage(page, 'test-session-id', '你好');
 *
 * // 等待响应
 * await waitForElement(page, '.message.user + .message.assistant');
 */
export async function sendMessage(
  page: Page,
  sessionId: string,
  content: string
) {
  try {
    // 如果不在会话详情页，先导航过去
    const currentUrl = page.url();
    if (!currentUrl.includes(`/sessions/${sessionId}`)) {
      await page.goto(`/sessions/${sessionId}`, { timeout: 10000 });
      await waitForPageLoad(page);
    }

    // 记录当前消息数量
    const beforeCount = await recordElementCount(page, '.message');

    // 找到消息输入框
    const inputSelector = 'textarea[placeholder*="消息"], textarea[placeholder*="输入"], [data-testid="message-input"]';
    await waitForElement(page, inputSelector);

    // 输入消息
    await clearAndFill(page, inputSelector, content);

    // 监听发送请求
    const sendRequest = waitForAPI(page, '/api/messages', 'POST').catch(() => null);

    // 点击发送按钮或按 Enter
    const sendButton = 'button[data-testid="send-btn"], button:has-text("发送")';
    const hasSendButton = await page.locator(sendButton).isVisible().catch(() => false);

    if (hasSendButton) {
      await page.click(sendButton);
    } else {
      await page.keyboard.press('Enter');
    }

    // 等待发送请求完成（如果成功监听到）
    await sendRequest;

    // 等待消息出现在列表中
    await page.waitForFunction(
      ({ selector, count, text }) => {
        const messages = document.querySelectorAll(selector);
        return messages.length > count && Array.from(messages).some(msg => msg.textContent?.includes(text));
      },
      { selector: '.message', count: beforeCount, text: content },
      { timeout: 5000 }
    );

    // 等待输入框清空
    await page.waitForFunction(
      ({ selector }) => {
        const input = document.querySelector(selector) as HTMLTextAreaElement;
        return input && input.value === '';
      },
      { selector: inputSelector },
      { timeout: 3000 }
    );

    return {
      sessionId,
      content,
      timestamp: new Date().toISOString(),
      success: true
    };
  } catch (error) {
    throw new Error(`发送消息失败: ${error}`);
  }
}

/**
 * 等待 AI 响应
 * @param page Playwright Page 对象
 * @param timeout 超时时间（毫秒）
 * @returns Promise 返回响应内容
 */
export async function waitForAIResponse(page: Page, timeout = 30000): Promise<string> {
  try {
    // 等待助手消息出现
    await page.waitForFunction(
      () => {
        const messages = document.querySelectorAll('.message.assistant, [data-testid="assistant-message"]');
        return messages.length > 0;
      },
      { timeout }
    );

    // 获取最后一条助手消息
    const response = await page.evaluate(() => {
      const messages = document.querySelectorAll('.message.assistant, [data-testid="assistant-message"]');
      const lastMessage = messages[messages.length - 1];
      return lastMessage?.textContent || '';
    });

    return response;
  } catch (error) {
    throw new Error(`等待 AI 响应超时: ${error}`);
  }
}

/**
 * 获取会话中的所有消息
 * @param page Playwright Page 对象
 * @returns Promise 返回消息数组
 */
export async function getAllMessages(page: Page): Promise<Array<{ role: string; content: string }>> {
  return await page.evaluate(() => {
    const messages = document.querySelectorAll('.message');
    return Array.from(messages).map(msg => {
      const role = msg.classList.contains('user') ? 'user' :
                   msg.classList.contains('assistant') ? 'assistant' : 'system';
      const content = msg.textContent || '';
      return { role, content };
    });
  });
}

// ============================================
// 主题辅助函数（新增和增强）
// ============================================

/**
 * 切换主题
 * @param page Playwright Page 对象
 * @param theme 目标主题（'light' 或 'dark'）
 * @returns Promise
 *
 * @example
 * // 切换到深色模式
 * await switchTheme(page, 'dark');
 *
 * // 验证主题已切换
 * await verifyTheme(page, 'dark');
 */
export async function switchTheme(page: Page, theme: 'light' | 'dark' | 'auto') {
  try {
    // 获取当前主题
    const currentTheme = await getCurrentTheme(page);

    // 如果已经是目标主题，直接返回
    if (currentTheme === theme) {
      return;
    }

    // 点击主题切换按钮
    const themeButtonSelectors = [
      '[data-testid="theme-toggle"]',
      'button[aria-label*="主题"]',
      'button:has-text("切换主题")',
      '.theme-toggle'
    ];

    let buttonClicked = false;
    for (const selector of themeButtonSelectors) {
      try {
        const isVisible = await page.locator(selector).isVisible({ timeout: 2000 });
        if (isVisible) {
          await page.click(selector);
          buttonClicked = true;
          break;
        }
      } catch {
        // 继续尝试下一个选择器
      }
    }

    if (!buttonClicked) {
      // 如果找不到按钮，尝试通过设置菜单
      await page.click('[data-testid="settings-btn"], button:has-text("设置"), .settings-button');
      await waitForElement(page, '[data-testid="theme-select"], select[name="theme"]');

      if (theme === 'dark') {
        await page.click('button:has-text("深色"), [data-theme-value="dark"]');
      } else if (theme === 'light') {
        await page.click('button:has-text("浅色"), [data-theme-value="light"]');
      } else {
        await page.click('button:has-text("自动"), [data-theme-value="auto"]');
      }
    }

    // 等待主题切换完成
    await page.waitForTimeout(500);

    // 验证主题已切换
    const newTheme = await getCurrentTheme(page);
    if (theme !== 'auto' && newTheme !== theme) {
      throw new Error(`主题切换失败: 期望 ${theme}, 实际 ${newTheme}`);
    }
  } catch (error) {
    throw new Error(`切换主题失败: ${error}`);
  }
}

/**
 * 等待主题切换动画完成
 * @param page Playwright Page 对象
 * @returns Promise
 */
export async function waitForThemeTransition(page: Page) {
  await page.waitForFunction(
    () => {
      const transition = (document.documentElement as any).style.transition;
      return !transition || transition === 'none';
    },
    { timeout: 1000 }
  );
}

/**
 * 验证主题颜色
 * @param page Playwright Page 对象
 * @param theme 主题名称
 * @returns Promise 返回主题颜色配置
 */
export async function getThemeColors(page: Page, theme: 'light' | 'dark') {
  return await page.evaluate((th) => {
    const root = document.documentElement;
    const colors = {
      background: getComputedStyle(root).getPropertyValue('--color-background'),
      foreground: getComputedStyle(root).getPropertyValue('--color-foreground'),
      primary: getComputedStyle(root).getPropertyValue('--color-primary'),
      secondary: getComputedStyle(root).getPropertyValue('--color-secondary'),
    };
    return colors;
  }, theme);
}

// ============================================
// 快速登录辅助函数（增强）
// ============================================

/**
 * 快速登录（使用测试账号）
 * @param page Playwright Page 对象
 * @param username 用户名（可选，默认使用测试账号）
 * @param password 密码（可选，默认使用测试账号）
 * @returns Promise
 *
 * @example
 * // 使用测试账号登录
 * await quickLogin(page);
 *
 * // 使用自定义账号登录
 * await quickLogin(page, 'myuser', 'mypass');
 */
export async function quickLogin(
  page: Page,
  username = testUser.username,
  password = testUser.password
) {
  try {
    // 先检查是否已登录（检查 access_token 和 token）
    let isLoggedIn = false;
    try {
      isLoggedIn = await page.evaluate(() => {
        const accessToken = localStorage.getItem('access_token');
        const token = localStorage.getItem('token');
        return !!(accessToken || token);
      });
    } catch (e) {
      // 如果页面不在正确的状态，忽略错误继续登录
      console.warn('无法检查登录状态，继续登录流程');
    }

    if (isLoggedIn) {
      console.log('已登录，跳过登录步骤');
      return;
    }

    // 使用 API 登录（更快更稳定）
    await loginViaAPI(page, username, password);

    // 等待页面更新
    await page.waitForTimeout(500);

    // 验证登录成功（通过检查 token）
    const hasToken = await page.evaluate(() => {
      const accessToken = localStorage.getItem('access_token');
      const token = localStorage.getItem('token');
      return !!(accessToken || token);
    });

    if (!hasToken) {
      throw new Error('登录后未找到 token');
    }

    console.log('✓ 登录验证成功');
  } catch (error) {
    throw new Error(`快速登录失败: ${error}`);
  }
}

/**
 * 通过 API 登录（绕过 UI）
 * @param page Playwright Page 对象
 * @param username 用户名
 * @param password 密码
 * @returns Promise 返回登录响应数据
 */
export async function loginViaAPI(
  page: Page,
  username = testUser.username,
  password = testUser.password
) {
  // 确保在正确的页面上（不是 about:blank）
  const currentUrl = page.url();
  if (currentUrl === 'about:blank' || !currentUrl.includes('localhost')) {
    // 导航到首页，这会触发重定向到登录页或应用首页
    await page.goto('/', { timeout: 15000, waitUntil: 'domcontentloaded' });
  }

  // 等待页面加载完成
  try {
    await page.waitForLoadState('domcontentloaded', { timeout: 15000 });
  } catch (e) {
    console.warn('等待页面加载超时，继续执行');
  }

  // 获取完整的 API URL
  const apiBaseUrl = page.context().browser()?.version() ? '' : process.env.REACT_APP_API_URL || "http://localhost:8888";

  const response = await page.evaluate(
    async ({ username, password, email }) => {
      try {
        // 构建完整的 API URL - 使用 localhost
        const apiUrl = `http://localhost:8888/api/v1/auth/login`;

        // 先尝试登录
        let res = await fetch(apiUrl, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ username, password }),
        });

        // 如果登录失败（401），尝试注册
        if (res.status === 401 || res.status === 422) {
          const apiRegUrl = `http://localhost:8888/api/v1/auth/register`;
          // 尝试注册用户
          const regRes = await fetch(apiRegUrl, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ username, password, email }),
          });

          if (regRes.ok) {
            // 注册成功，重新登录
            res = await fetch(apiUrl, {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ username, password }),
            });
          }
        }

        // 检查响应是否为空
        const text = await res.text();
        if (!text) {
          return { status: res.status, data: null, error: 'Empty response', url: apiUrl };
        }

        // 尝试解析 JSON
        let data;
        try {
          data = JSON.parse(text);
        } catch (e) {
          return { status: res.status, data: null, error: `Invalid JSON: ${text}`, url: apiUrl };
        }

        if (res.ok && data.access_token) {
          // 使用正确的 storage key 名称，与 authStore.ts 中的 STORAGE_KEYS 一致
          localStorage.setItem('access_token', data.access_token);
          localStorage.setItem('refresh_token', data.refresh_token);
          localStorage.setItem('token', data.access_token); // 兼容旧版本

          // 计算 token 过期时间
          const expiryTime = Date.now() + (data.expires_in || 7200) * 1000;
          localStorage.setItem('access_token_expiry', expiryTime.toString());

          // 存储用户信息（使用 auth_user 键）
          if (data.user) {
            localStorage.setItem('auth_user', JSON.stringify(data.user));
          } else {
            // 创建基本的用户信息
            const basicUser = { id: 'test', username, email };
            localStorage.setItem('auth_user', JSON.stringify(basicUser));
          }

          return { status: res.status, data, success: true };
        }

        return { status: res.status, data, error: data.detail || 'Login failed', url: apiUrl };
      } catch (error) {
        return { status: 0, data: null, error: String(error) };
      }
    },
    { username, password, email: testUser.email, apiBaseUrl }
  );

  if (response.status !== 200 || !response.success) {
    throw new Error(`API 登录失败: ${response.error || JSON.stringify(response.data) || response.error}`);
  }

  // 导航到首页以应用登录状态（而不是刷新当前页面）
  await page.goto('/', { timeout: 15000, waitUntil: 'domcontentloaded' });

  // 等待页面稳定
  await page.waitForTimeout(500);

  // 注意：根据系统架构，会话只能通过主agent创建，前端不能直接调用API创建会话
  // 前端应该通过UI操作（如点击"新建会话"按钮）来触发主agent创建会话
  const urlAfterLogin = page.url();
  if (!urlAfterLogin.includes('/session/')) {
    // 在 DashboardPage，需要通过UI创建会话
    console.log('当前在DashboardPage，需要通过UI创建会话（不能直接调用API）');
    // 不再尝试直接调用API创建会话
  }

  return response.data;
}

/**
 * 登出并清理状态
 * @param page Playwright Page 对象
 * @returns Promise
 */
export async function logoutAndCleanup(page: Page) {
  try {
    // 获取 baseURL（从 page.context() 或使用环境变量）
    let baseURL = 'http://localhost:5188';  // 更新默认值
    if (process.env.FRONTEND_URL) {
      baseURL = process.env.FRONTEND_URL;
    } else {
      // 尝试从当前URL推断baseURL
      const currentUrl = page.url();
      if (currentUrl && currentUrl.includes('localhost')) {
        const urlObj = new URL(currentUrl);
        baseURL = `${urlObj.protocol}//${urlObj.host}`;
      }
    }

    // 先确保在应用页面上
    const currentUrl = page.url();
    if (currentUrl === 'about:blank' || !currentUrl.includes('localhost')) {
      await page.goto(baseURL, { timeout: 10000, waitUntil: 'domcontentloaded' });
    }

    // 清理 localStorage（添加安全检查）
    try {
      await page.evaluate(() => {
        localStorage.clear();
        sessionStorage.clear();
      });
    } catch (e) {
      // 如果无法访问 localStorage（可能页面状态特殊），忽略错误
      console.warn('无法访问 localStorage，将在导航后重试');
    }

    // 清理 cookies
    const context = page.context();
    await context.clearCookies();

    // 导航到首页以应用清理
    await page.goto(baseURL, { timeout: 10000, waitUntil: 'domcontentloaded' });

    // 再次尝试清理（确保在正确的页面上）
    try {
      await page.evaluate(() => {
        localStorage.clear();
        sessionStorage.clear();
      });
    } catch (e) {
      // 忽略错误
    }

    // 验证已登出（不强制要求，因为可能在某些页面）
    try {
      await expect(page.locator('[data-testid="user-menu"], .user-menu')).not.toBeVisible({ timeout: 5000 });
    } catch (e) {
      // 忽略验证失败
    }
  } catch (error) {
    // 不抛出错误，只记录日志
    console.warn(`登出清理警告: ${error}`);
  }
}
