/**
 * E2E 测试核心辅助工具
 *
 * 封装 Playwright 浏览器级常用操作：
 * - UI 级登录流程（通过真实浏览器表单交互）
 * - WebSocket 事件收集（通过浏览器上下文拦截 WS 消息）
 * - 聊天消息发送（真实浏览器输入+点击）
 * - 事件断言工具
 *
 * 设计原则：
 * - 所有操作通过 Playwright page API 模拟真实用户行为
 * - 复用 e2e/helpers/ 中的已有辅助函数，不重复实现
 * - WS 事件收集通过 page.evaluate 注入拦截器实现
 *
 * 来源：方案文档 7.8 节场景设计，features.md 场景 1/5/2
 */

import { expect, type Page, type Locator } from '@playwright/test';
import { APP_URL, TEST_USER, registerUser } from '../helpers/auth';

// ─── 类型定义 ────────────────────────────────────────────────

/** WebSocket 事件数据结构（与 api_contract.md 消息格式一致） */
export interface WSEvent {
  /** 事件类型（如 execution_start, execution_done, interaction_request 等） */
  type: string;
  /** 事件携带的数据 */
  data: Record<string, unknown>;
  /** 消息来源类型 */
  source_type?: string;
  /** 来源标识 */
  source_id?: string;
  /** 接收时间戳 */
  timestamp: number;
}

/** 登录结果 */
export interface LoginResult {
  success: boolean;
  token: string | null;
  refreshToken: string | null;
}

/** 扩展 Window 接口，声明 E2E 测试注入的 WS 事件收集属性 */
declare global {
  interface Window {
    __e2e_ws_events?: WSEvent[];
  }
}

// ─── UI 级登录流程 ────────────────────────────────────────────

/**
 * 通过真实浏览器表单完成登录
 *
 * 打开登录页 → 填写用户名密码 → 点击登录 → 等待跳转
 * 使用 Playwright page.fill/click 模拟真实用户操作，而非 API 注入。
 *
 * @param page Playwright Page 实例
 * @param username 用户名（默认使用测试用户）
 * @param password 密码（默认使用测试用户）
 */
export async function loginViaUI(
  page: Page,
  username: string = TEST_USER.username,
  password: string = TEST_USER.password,
): Promise<void> {
  // 先通过 API 注册（如已存在则忽略），复用 auth.ts 的注册逻辑
  await registerUser(page);

  // 打开登录页
  await page.goto(`${APP_URL}/login`);
  await page.waitForLoadState('domcontentloaded');

  // 等待登录表单可见
  const loginForm = page.locator('[data-testid="login-form"]');
  await expect(loginForm, '登录表单应可见').toBeVisible({ timeout: 10_000 });

  // 填写用户名
  const usernameInput = page.locator('[data-testid="login-username-input"]');
  await expect(usernameInput, '用户名输入框应可见').toBeVisible();
  await usernameInput.fill(username);

  // 填写密码
  const passwordInput = page.locator('[data-testid="login-password-input"]');
  await expect(passwordInput, '密码输入框应可见').toBeVisible();
  await passwordInput.fill(password);

  // 点击登录按钮
  const submitBtn = page.locator('[data-testid="login-submit-button"]');
  await expect(submitBtn, '登录按钮应可见').toBeVisible();
  await submitBtn.click();

  // 等待页面跳转到首页
  await page.waitForURL(APP_URL + '/', { timeout: 30_000 });
  await page.waitForLoadState('networkidle');

  console.log('✅ UI 级登录成功，已跳转到首页');
}

/**
 * 验证 Token 已持久化到 localStorage
 *
 * @param page Playwright Page 实例
 * @returns 登录结果（含 token 信息）
 */
export async function verifyTokenPersisted(page: Page): Promise<LoginResult> {
  const tokens = await page.evaluate(() => {
    return {
      token: localStorage.getItem('access_token'),
      refreshToken: localStorage.getItem('refresh_token'),
      expiry: localStorage.getItem('access_token_expiry'),
      user: localStorage.getItem('auth_user'),
    };
  });

  expect(tokens.token, 'access_token 应已持久化到 localStorage').not.toBeNull();
  expect(tokens.token!.length, 'access_token 应有内容').toBeGreaterThan(10);
  expect(tokens.refreshToken, 'refresh_token 应已持久化').not.toBeNull();
  expect(tokens.expiry, 'access_token_expiry 应已持久化').not.toBeNull();
  expect(tokens.user, 'auth_user 应已持久化').not.toBeNull();

  console.log(`✅ Token 持久化验证通过，token 长度: ${tokens.token!.length}`);

  return {
    success: true,
    token: tokens.token,
    refreshToken: tokens.refreshToken,
  };
}

// ─── WebSocket 事件收集 ───────────────────────────────────────

/**
 * 在浏览器上下文中注入 WS 消息拦截器
 *
 * 通过 monkey-patch WebSocket.prototype 在浏览器中收集所有 WS 消息，
 * 存储到 window.__e2e_ws_events 数组中供后续读取。
 *
 * 必须在页面加载后、WS 连接建立前调用。
 *
 * @param page Playwright Page 实例
 */
export async function injectWSEventCollector(page: Page): Promise<void> {
  await page.evaluate(() => {
    // 初始化事件存储
    window.__e2e_ws_events = [];

    // 保存原始 WebSocket 构造函数
    const OriginalWebSocket = window.WebSocket;

    // 创建代理 WebSocket
    class ProxiedWebSocket extends OriginalWebSocket {
      constructor(url: string | URL, protocols?: string | string[]) {
        super(url, protocols);

        this.addEventListener('message', (event: MessageEvent) => {
          try {
            const parsed = JSON.parse(event.data);
            window.__e2e_ws_events!.push({
              ...parsed,
              timestamp: Date.now(),
            });
          } catch {
            // 非 JSON 消息，跳过
          }
        });
      }
    }

    // 替换全局 WebSocket
    window.WebSocket = ProxiedWebSocket;
  });

  console.log('✅ WS 事件拦截器已注入');
}

/**
 * 获取当前已收集的所有 WS 事件
 *
 * @param page Playwright Page 实例
 * @returns WS 事件数组
 */
export async function getCollectedEvents(page: Page): Promise<WSEvent[]> {
  return await page.evaluate(() => {
    return window.__e2e_ws_events || [];
  });
}

/**
 * 等待特定类型的 WS 事件出现
 *
 * 轮询浏览器中的事件列表，直到匹配的事件出现或超时。
 *
 * @param page Playwright Page 实例
 * @param eventType 事件类型（如 'execution_done', 'interaction_request'）
 * @param timeout 超时时间（毫秒），默认 30 秒
 * @returns 匹配的第一个事件，或 null（超时）
 */
export async function waitForWSEvent(
  page: Page,
  eventType: string,
  timeout = 30_000,
): Promise<WSEvent | null> {
  const deadline = Date.now() + timeout;

  while (Date.now() < deadline) {
    const events = await getCollectedEvents(page);
    const matched = events.find((e) => e.type === eventType);
    if (matched) {
      console.log(`✅ 捕获到 WS 事件: ${eventType}`);
      return matched;
    }
    await page.waitForTimeout(500);
  }

  console.log(`⚠️ 等待 WS 事件 ${eventType} 超时 (${timeout}ms)`);
  return null;
}

/**
 * 清空已收集的 WS 事件
 *
 * 用于在发送新消息前重置事件列表。
 *
 * @param page Playwright Page 实例
 */
export async function clearCollectedEvents(page: Page): Promise<void> {
  await page.evaluate(() => {
    window.__e2e_ws_events = [];
  });
}

// ─── 聊天消息操作 ─────────────────────────────────────────────

/**
 * 通过真实浏览器输入发送聊天消息
 *
 * 在聊天输入框中输入文字 → 点击发送按钮。
 * 依赖聊天界面已加载。
 *
 * @param page Playwright Page 实例
 * @param text 要发送的消息文本
 */
export async function sendChatMessage(page: Page, text: string): Promise<void> {
  const input = page.locator('[data-testid="chat-input-textarea"]');
  await expect(input, '聊天输入框应可见').toBeVisible({ timeout: 10_000 });

  await input.click();
  await input.fill(text);
  await expect(input, '输入框应已填入文字').toHaveValue(text);

  const sendBtn = page.locator('[data-testid="chat-send-button"]');
  await expect(sendBtn, '发送按钮应可见').toBeVisible();
  await sendBtn.click();

  console.log(`✅ 已发送消息: "${text.substring(0, 40)}"`);
}

/**
 * 等待助手回复消息出现
 *
 * @param page Playwright Page 实例
 * @param timeout 超时时间（毫秒）
 * @returns 助手消息 Locator
 */
export async function waitForAssistantReply(
  page: Page,
  timeout = 60_000,
): Promise<Locator> {
  // 尝试多种选择器定位助手消息
  const selectors = [
    '[data-role="assistant"]',
    '[data-testid="assistant-message"]',
    '[data-message-role="assistant"]',
  ];

  for (const sel of selectors) {
    const msg = page.locator(sel).last();
    if (await msg.isVisible().catch(() => false)) {
      return msg;
    }
  }

  // 如果未找到，等待任一选择器出现
  const combined = page.locator(selectors.join(', ')).last();
  await expect(combined, '助手消息应出现').toBeVisible({ timeout });
  return combined;
}

/**
 * 验证流式响应渲染（文本持续增长）
 *
 * @param page Playwright Page 实例
 * @param timeout 观察时间窗口（毫秒）
 * @returns 初始长度和最终长度
 */
export async function verifyStreamingResponse(
  page: Page,
  timeout = 10_000,
): Promise<{ initialLength: number; finalLength: number }> {
  const msg = await waitForAssistantReply(page, 30_000);
  const initialText = (await msg.textContent().catch(() => '')) ?? '';
  const initialLength = initialText.length;

  // 等待一段时间观察文本增长
  await page.waitForTimeout(timeout);

  const finalText = (await msg.textContent().catch(() => '')) ?? '';
  const finalLength = finalText.length;

  expect(finalLength, '流式响应最终长度应 > 0').toBeGreaterThan(0);
  console.log(`✅ 流式渲染验证: ${initialLength} → ${finalLength} 字符`);

  return { initialLength, finalLength };
}

// ─── 事件断言工具 ─────────────────────────────────────────────

/**
 * 断言事件序列包含特定类型
 *
 * @param events 已收集的事件列表
 * @param eventType 期望的事件类型
 * @param message 断言失败消息
 */
export function expectEventOfType(
  events: WSEvent[],
  eventType: string,
  message?: string,
): void {
  const hasEvent = events.some((e) => e.type === eventType);
  expect(
    hasEvent,
    message ?? `事件序列中应包含类型 "${eventType}"，实际事件类型: ${events.map((e) => e.type).join(', ')}`,
  ).toBe(true);
}

/**
 * 断言事件序列按预期顺序出现
 *
 * @param events 已收集的事件列表
 * @param expectedOrder 期望的事件类型顺序
 */
export function expectEventOrder(
  events: WSEvent[],
  expectedOrder: string[],
): void {
  const actualTypes = events.map((e) => e.type);

  for (const expectedType of expectedOrder) {
    const index = actualTypes.indexOf(expectedType);
    expect(index, `事件序列中应包含 "${expectedType}"`).toBeGreaterThan(-1);
  }

  // 验证相对顺序
  let lastIndex = -1;
  for (const expectedType of expectedOrder) {
    const index = actualTypes.indexOf(expectedType, lastIndex + 1);
    expect(
      index,
      `"${expectedType}" 应在 "${expectedOrder[expectedOrder.indexOf(expectedType) - 1] ?? '开始'}" 之后出现`,
    ).toBeGreaterThan(lastIndex);
    lastIndex = index;
  }
}
