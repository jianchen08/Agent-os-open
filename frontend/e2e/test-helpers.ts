/**
 * 统一的 E2E 测试辅助函数
 *
 * 整合所有前端测试常用的辅助函数，消除重复代码
 */

import { Page, Locator, expect } from '@playwright/test';

// ============================================
// 测试配置常量
// ============================================

/**
 * 测试用户凭据
 */
export const TEST_USER = {
  username: 'admin',
  password: 'admin123456',
  email: 'admin@example.com',
} as const;

/**
 * 测试超时配置
 */
export const TIMEOUTS = {
  NAVIGATION: 30000,
  ELEMENT_VISIBLE: 10000,
  NETWORK_IDLE: 30000,
  TASK_COMPLETION: 60000,
  API_RESPONSE: 10000,
} as const;

/**
 * 测试 URL
 */
export const URLS = {
  BASE: 'http://localhost:5188',
  LOGIN: '/login',
  DASHBOARD: '/',
  SESSION: '/session',
} as const;

// ============================================
// 认证相关辅助函数
// ============================================

/**
 * 执行登录
 * @param page Playwright Page 对象
 * @param username 用户名
 * @param password 密码
 */
export async function login(
  page: Page,
  username = TEST_USER.username,
  password = TEST_USER.password
) {
  // 导航到登录页面
  await page.goto(`${URLS.BASE}${URLS.LOGIN}`, { timeout: TIMEOUTS.NAVIGATION });

  // 填写用户名
  await page.fill(
    'input[id="username"], [data-testid="login-username-input"]',
    username,
    { timeout: TIMEOUTS.ELEMENT_VISIBLE }
  );

  // 填写密码
  await page.fill(
    'input[id="password"], [data-testid="login-password-input"]',
    password,
    { timeout: TIMEOUTS.ELEMENT_VISIBLE }
  );

  // 点击登录按钮
  await page.click(
    'button[type="submit"], [data-testid="login-submit-button"]',
    { timeout: TIMEOUTS.ELEMENT_VISIBLE }
  );

  // 等待导航到首页或会话页面
  await page.waitForURL(/\/(session\/[a-f0-9-]+)?/, { timeout: TIMEOUTS.NAVIGATION }).catch(() => {
    // 如果 URL 没有变化，可能已经在目标页面了
    return page.locator('body').isVisible({ timeout: 5000 });
  });

  // 等待页面加载完成
  await waitForPageLoad(page);
}

/**
 * 执行登出
 * @param page Playwright Page 对象
 */
export async function logout(page: Page) {
  await page.click('[data-testid="user-menu"], .user-menu');
  await page.click('button:has-text("退出"), button:has-text("登出")');
}

/**
 * 检查是否已登录
 * @param page Playwright Page 对象
 */
export async function isLoggedIn(page: Page): Promise<boolean> {
  const url = page.url();
  return !url.includes('/login');
}

// ============================================
// 页面操作辅助函数
// ============================================

/**
 * 等待页面加载完成
 * @param page Playwright Page 对象
 */
export async function waitForPageLoad(page: Page) {
  await page.waitForLoadState('networkidle', { timeout: TIMEOUTS.NETWORK_IDLE }).catch(() => {
    // networkidle 可能永远等不到，忽略错误
  });
  await page.waitForLoadState('domcontentloaded');
}

/**
 * 截图并保存
 * @param page Playwright Page 对象
 * @param name 截图名称
 * @param fullPage 是否截取完整页面
 */
export async function takeScreenshot(page: Page, name: string, fullPage = true) {
  await page.screenshot({
    path: `screenshots/${name}.png`,
    fullPage,
  });
}

/**
 * 填写表单
 * @param page Playwright Page 对象
 * @param fields 表单字段键值对
 */
export async function fillForm(page: Page, fields: Record<string, string>) {
  for (const [selector, value] of Object.entries(fields)) {
    await page.fill(selector, value);
  }
}

/**
 * 等待元素可见并可点击
 * @param page Playwright Page 对象
 * @param selector 元素选择器
 * @param timeout 超时时间
 */
export async function waitAndClick(page: Page, selector: string, timeout = TIMEOUTS.ELEMENT_VISIBLE) {
  await page.waitForSelector(selector, { state: 'visible', timeout });
  await page.click(selector);
}

/**
 * 检查 toast 消息
 * @param page Playwright Page 对象
 * @param message 消息内容
 */
export async function checkToast(page: Page, message: string) {
  const toast = page.locator(`.toast, [role="alert"]`).filter({ hasText: message });
  await expect(toast).toBeVisible({ timeout: 5000 });
}

/**
 * 模拟文件上传
 * @param page Playwright Page 对象
 * @param selector 文件输入选择器
 * @param filePath 文件路径
 */
export async function uploadFile(page: Page, selector: string, filePath: string) {
  const fileInput = page.locator(selector);
  await fileInput.setInputFiles(filePath);
}

// ============================================
// 存储操作辅助函数
// ============================================

/**
 * 获取存储状态
 * @param page Playwright Page 对象
 * @param key 存储键
 */
export async function getStorageState(page: Page, key: string): Promise<string | null> {
  return await page.evaluate((k) => {
    return localStorage.getItem(k);
  }, key);
}

/**
 * 设置存储状态
 * @param page Playwright Page 对象
 * @param state 存储状态对象
 */
export async function setStorageState(page: Page, state: Record<string, string>) {
  await page.evaluate((s) => {
    for (const [key, value] of Object.entries(s)) {
      localStorage.setItem(key, value);
    }
  }, state);
}

/**
 * 清除存储状态
 * @param page Playwright Page 对象
 */
export async function clearStorageState(page: Page) {
  await page.evaluate(() => {
    localStorage.clear();
    sessionStorage.clear();
  });
}

// ============================================
// 网络请求辅助函数
// ============================================

/**
 * 监听 API 响应
 * @param page Playwright Page 对象
 * @param urlPattern URL 匹配模式
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
 * @param page Playwright Page 对象
 * @param endpoint API 端点
 */
export async function waitForPOSTRequest(page: Page, endpoint: string) {
  return page.waitForRequest(
    (request) => request.method() === 'POST' && request.url().includes(endpoint)
  );
}

/**
 * 监听 GET 请求
 * @param page Playwright Page 对象
 * @param endpoint API 端点
 */
export async function waitForGETRequest(page: Page, endpoint: string) {
  return page.waitForRequest(
    (request) => request.method() === 'GET' && request.url().includes(endpoint)
  );
}

// ============================================
// 任务相关辅助类
// ============================================

/**
 * 任务辅助类
 */
export class TaskHelpers {
  constructor(private page: Page) {}

  /**
   * 创建任务
   * @param goal 任务目标
   * @param waitForCompletion 是否等待任务完成
   * @returns 任务 ID
   */
  async createTask(goal: string, waitForCompletion = false): Promise<string | null> {
    // 填写任务目标
    const chatInput = this.page.locator('textarea[placeholder*="消息"], [data-testid="chat-input"]');
    await chatInput.fill(goal);

    // 监听创建任务 API
    const createTaskRequest = this.page.waitForRequest(
      (req) => req.url().includes('/api/tasks') && req.method() === 'POST',
      { timeout: TIMEOUTS.API_RESPONSE }
    ).catch(() => null);

    // 点击发送按钮
    const sendButton = this.page.locator('button:has-text("发送"), [data-testid="send-button"]');
    await sendButton.click();

    // 等待任务卡片出现
    await this.page.waitForSelector('[data-testid="task-card"]', { timeout: TIMEOUTS.API_RESPONSE });

    // 如果需要等待任务完成
    if (waitForCompletion) {
      await this.waitForTaskCompletion(goal);
    }

    // 返回任务 ID
    if (createTaskRequest) {
      const url = createTaskRequest.url();
      const taskId = await this.page.evaluate(async (requestUrl) => {
        const response = await fetch(requestUrl);
        const data = await response.json();
        return data.id || data.taskId;
      }, url);

      return taskId;
    }

    return null;
  }

  /**
   * 等待任务达到指定阶段
   * @param taskId 任务 ID
   * @param phase 阶段名称
   * @param timeout 超时时间
   */
  async waitForTaskPhase(
    taskId: string,
    phase: 'prepare' | 'execute' | 'evaluate',
    timeout = TIMEOUTS.TASK_COMPLETION
  ) {
    await this.page.waitForSelector(
      `[data-testid="task-card"][data-task-id="${taskId}"] [data-phase="${phase}"]`,
      { state: 'visible', timeout }
    );
  }

  /**
   * 等待任务完成
   * @param goal 任务目标
   * @param timeout 超时时间
   */
  async waitForTaskCompletion(goal: string, timeout = TIMEOUTS.TASK_COMPLETION) {
    const startTime = Date.now();

    while (Date.now() - startTime < timeout) {
      // 查找包含指定目标的任务卡片
      const taskCard = this.page.locator(`[data-testid="task-card"]`).filter({ hasText: goal });

      // 检查是否有完成状态
      const completedStatus = taskCard.locator(
        '[data-status="completed"], [data-phase="evaluate"][data-phase-status="completed"]'
      );
      const isCompleted = await completedStatus.count() > 0;

      if (isCompleted) {
        return;
      }

      // 等待一段时间再检查
      await this.page.waitForTimeout(2000);
    }

    throw new Error(`任务完成超时: ${goal}`);
  }

  /**
   * 获取任务卡片元素
   * @param taskId 任务 ID
   */
  getTaskCard(taskId: string): Locator {
    return this.page.locator(`[data-testid="task-card"][data-task-id="${taskId}"]`);
  }

  /**
   * 获取任务当前阶段
   * @param taskId 任务 ID
   */
  async getTaskCurrentPhase(taskId: string): Promise<string | null> {
    const taskCard = this.getTaskCard(taskId);
    const currentPhase = taskCard.locator('[data-testid="current-phase"]');

    const phaseText = await currentPhase.textContent().catch(() => null);
    return phaseText;
  }

  /**
   * 点击 Agent Tab
   * @param agentName Agent 名称
   */
  async clickAgentTab(agentName: string) {
    const tab = this.page.locator(`[data-testid="agent-tab"][data-agent-name="${agentName}"]`);
    await tab.click();
  }

  /**
   * 点击 Agent Tab（通过索引）
   * @param index Tab 索引
   */
  async clickAgentTabByIndex(index: number) {
    const tabs = this.page.locator('[data-testid="agent-tab"]');
    await tabs.nth(index).click();
  }

  /**
   * 获取当前活跃的 Agent Tab
   */
  getActiveAgentTab(): Locator {
    return this.page.locator(
      '[data-testid="agent-tab"][data-active="true"], [data-testid="agent-tab"].active'
    );
  }
}

// ============================================
// 导出便捷函数
// ============================================

/**
 * 创建 TaskHelpers 实例
 * @param page Playwright Page 对象
 */
export function createTaskHelpers(page: Page): TaskHelpers {
  return new TaskHelpers(page);
}
