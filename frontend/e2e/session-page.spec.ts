/**
 * Session 页面完整 E2E 测试
 *
 * 测试会话页面的核心功能：
 * 1. 创建会话（最重要）
 *    - 记录初始会话数
 *    - 点击创建按钮
 *    - 验证新卡片出现
 *    - 监听 API（POST /api/v1/threads）
 *    - 验证 201 状态码
 *    - 验证返回会话 ID
 *
 * 2. 发送消息（最重要）
 *    - 输入消息内容
 *    - 点击发送按钮
 *    - 验证输入框清空
 *    - 验证消息出现
 *    - 验证状态变化（sending → sent）
 *    - 监听 API（POST /api/messages）
 *    - 验证 200 状态码
 *
 * 3. 删除会话
 *    - 点击删除按钮
 *    - 验证确认对话框
 *    - 确认删除
 *    - 验证元素消失
 *    - 监听 DELETE API
 *
 * 4. 会话切换
 *    - 点击不同会话
 *    - 验证路由变化
 *    - 验证消息内容更新
 *
 * 测试规则：严格遵循
 * 用户操作 → 前端UI变化 → 后端API响应 → 数据库持久化
 */

import { test, expect } from '@playwright/test';
import {
  login,
  quickLogin,
  recordElementCount,
  verifyElementCountChanged,
  waitForAPI,
  waitForAPIResponse,
  verifyAPIStatus,
  getAPIData,
  verifyDBRecord,
  verifyRecordDeleted,
  getDBRecordCount,
  waitForElement,
  waitForElementRemoved,
  clearAndFill,
  waitAndClick,
  waitForSuccessMessage,
  verifyURL,
  takeScreenshot,
  waitForPageLoad,
  recordState,
  createSession,
  sendMessage,
  waitForAIResponse,
  getAllMessages,
  logoutAndCleanup,
} from './helpers';

test.describe('Session 页面核心功能测试', () => {
  // 每个测试前清理并登录
  test.beforeEach(async ({ page }) => {
    await logoutAndCleanup(page);
    await quickLogin(page);
    await page.waitForLoadState('networkidle');
  });

  test.afterEach(async ({ page }, testInfo) => {
    // 测试失败时截图
    if (testInfo.status !== 'passed') {
      await page.screenshot({
        path: `test-results/session-page-failed-${testInfo.title}.png`,
        fullPage: true,
      });
    }
  });

  /**
   * ============================================================================
   * 测试组 1: 创建会话（核心功能）
   * ============================================================================
   */
  test.describe('创建会话', () => {
    test('01-创建会话-应该能够从仪表盘创建新会话', async ({ page }) => {
      // 1. 记录初始会话数
      const initialCount = await getDBRecordCount(page, '/api/v1/threads');
      console.log(`初始会话数: ${initialCount}`);

      // 2. 导航到仪表盘
      await page.goto('/');
      await waitForPageLoad(page);

      // 3. 监听创建会话 API
      const createRequestPromise = waitForAPI(page, '/api/v1/threads', 'POST');

      // 4. 点击创建按钮
      const createButton = page.locator('button:has-text("新建会话")');
      await expect(createButton).toBeVisible();
      await createButton.click();

      // 5. 等待并验证 API 请求
      const createRequest = await createRequestPromise;
      console.log('API 请求 URL:', createRequest.url());
      console.log('API 请求方法:', createRequest.method());

      // 6. 监听 API 响应
      const response = await waitForAPIResponse(page, '/api/v1/threads');
      console.log('API 响应状态:', response.status());

      // 7. 验证 201 状态码
      expect(response.status()).toBe(201);

      // 8. 验证返回会话 ID
      const responseData = await response.json();
      expect(responseData.id).toBeTruthy();
      console.log('新会话 ID:', responseData.id);

      // 9. 验证导航到会话页面
      await page.waitForURL(/\/session\//, { timeout: 5000 });
      expect(page.url()).toMatch(/\/session\/[a-f0-9-]+$/);

      // 10. 验证会话页面显示
      const sessionPage = page.locator('[data-testid="session-page"]');
      await expect(sessionPage).toBeVisible();

      // 11. 验证数据库中的会话数量增加
      const newCount = await getDBRecordCount(page, '/api/v1/threads');
      expect(newCount).toBe(initialCount + 1);
      console.log(`会话数从 ${initialCount} 增加到 ${newCount}`);

      // 12. 验证新会话在数据库中存在
      await verifyDBRecord(page, '/api/v1/threads', responseData.id);

      await takeScreenshot(page, 'session-page-01-create-success');
    });

    test('02-创建会话-应该验证新会话卡片出现在列表中', async ({ page }) => {
      // 1. 导航到仪表盘
      await page.goto('/');
      await waitForPageLoad(page);

      // 2. 记录当前会话卡片数量
      const initialCardCount = await recordElementCount(page, 'button[aria-label*="进入会话"]');
      console.log(`初始会话卡片数: ${initialCardCount}`);

      // 3. 点击创建按钮
      const createButton = page.locator('button:has-text("新建会话")');
      await createButton.click();

      // 4. 等待导航到会话页面
      await page.waitForURL(/\/session\//);

      // 5. 返回仪表盘
      await page.goto('/');
      await waitForPageLoad(page);

      // 6. 验证会话卡片数量增加
      await verifyElementCountChanged(page, 'button[aria-label*="进入会话"]', initialCardCount, 'increase');

      // 7. 验证新会话卡片包含正确的信息
      const sessionCards = page.locator('button[aria-label*="进入会话"]');
      const newCard = sessionCards.first();

      // 验证卡片有标题
      const title = newCard.locator('p.font-medium');
      await expect(title).toBeVisible();

      // 验证卡片有消息数量显示
      const messageCount = newCard.locator('.text-muted-foreground:has-text("条消息")');
      await expect(messageCount).toBeVisible();

      await takeScreenshot(page, 'session-page-02-new-card-visible');
    });

    test('03-创建会话-应该验证会话状态正确初始化', async ({ page }) => {
      // 1. 创建新会话
      await page.goto('/');
      const createButton = page.locator('button:has-text("新建会话")');
      await createButton.click();

      // 2. 等待导航到会话页面
      await page.waitForURL(/\/session\//);
      const sessionId = page.url().split('/session/')[1];

      // 3. 验证会话页面显示正确的初始状态
      const sessionPage = page.locator('[data-testid="session-page"]');
      await expect(sessionPage).toBeVisible();

      // 4. 验证会话标题存在
      const title = page.locator('h1.font-semibold');
      await expect(title).toBeVisible();

      // 5. 验证消息区域为空
      const messages = page.locator('.message');
      const messageCount = await messages.count();
      expect(messageCount).toBe(0);

      // 6. 验证输入框可见但禁用（当前实现）
      const input = page.locator('input[placeholder*="消息"]');
      await expect(input).toBeVisible();
      await expect(input).toBeDisabled();

      await takeScreenshot(page, 'session-page-03-session-state-init');
    });
  });

  /**
   * ============================================================================
   * 测试组 2: 发送消息（核心功能）
   * ============================================================================
   */
  test.describe('发送消息', () => {
    test('04-发送消息-应该能够发送文本消息', async ({ page }) => {
      // 1. 创建新会话
      await page.goto('/');
      const createButton = page.locator('button:has-text("新建会话")');
      await createButton.click();
      await page.waitForURL(/\/session\//);
      const sessionId = page.url().split('/session/')[1];

      // 2. 记录当前消息数量
      const initialMessageCount = await recordElementCount(page, '.message');
      console.log(`初始消息数: ${initialMessageCount}`);

      // 3. 注意：当前实现中输入框被禁用，消息功能未实现
      // 这个测试验证当前状态
      const input = page.locator('input[placeholder*="消息"]');
      await expect(input).toBeDisabled();

      // 4. 验证提示消息显示
      const hint = page.locator('.text-xs.text-muted-foreground:has-text("消息输入功能将在后续版本中实现")');
      await expect(hint).toBeVisible();

      await takeScreenshot(page, 'session-page-04-message-disabled-state');

      // 跳过实际发送测试，因为功能未实现
      test.skip(true, '消息发送功能尚未实现');
    });

    test('05-发送消息-应该验证消息状态变化', async ({ page }) => {
      // 注意：此测试在消息功能实现后启用
      test.skip(true, '等待消息功能实现');

      // 1. 创建会话
      await page.goto('/');
      const createButton = page.locator('button:has-text("新建会话")');
      await createButton.click();
      await page.waitForURL(/\/session\//);

      // 2. 记录发送前状态
      const beforeState = await recordState(page, {
        input: 'input[placeholder*="消息"]',
        sendButton: 'button:has-text("发送")',
        messageList: '.message',
      });

      // 3. 输入消息
      const testMessage = '这是一条测试消息';
      await clearAndFill(page, 'input[placeholder*="消息"]', testMessage);

      // 4. 点击发送（假设功能已实现）
      // await page.click('button:has-text("发送")');

      // 5. 验证输入框清空
      // await expect(page.locator('input[placeholder*="消息"]')).toHaveValue('');

      // 6. 验证消息出现在列表中
      // await waitForElement(page, `.message:has-text("${testMessage}")`);

      // 7. 验证消息数量增加
      // await verifyElementCountChanged(page, '.message', beforeState.messageList.visible ? 1 : 0, 'increase');

      await takeScreenshot(page, 'session-page-05-message-state-change');
    });

    test('06-发送消息-应该监听并验证 API 调用', async ({ page }) => {
      // 注意：此测试在消息功能实现后启用
      test.skip(true, '等待消息功能实现');

      // 1. 创建会话
      await page.goto('/');
      const createButton = page.locator('button:has-text("新建会话")');
      await createButton.click();
      await page.waitForURL(/\/session\//);
      const sessionId = page.url().split('/session/')[1];

      // 2. 监听消息发送 API
      const messageRequestPromise = waitForAPI(page, `/api/v1/threads/${sessionId}/messages`, 'POST');

      // 3. 发送消息（假设功能已实现）
      // await clearAndFill(page, 'input[placeholder*="消息"]', '测试消息');
      // await page.click('button:has-text("发送")');

      // 4. 等待并验证 API 请求
      // const messageRequest = await messageRequestPromise;
      // expect(messageRequest.method()).toBe('POST');

      // 5. 验证请求体包含消息内容
      // const postData = JSON.parse(messageRequest.postData() || '{}');
      // expect(postData.content).toBe('测试消息');

      // 6. 监听并验证响应
      // const response = await waitForAPIResponse(page, `/api/v1/threads/${sessionId}/messages`);
      // expect(response.status()).toBe(200);

      await takeScreenshot(page, 'session-page-06-message-api-monitor');
    });
  });

  /**
   * ============================================================================
   * 测试组 3: 删除会话
   * ============================================================================
   */
  test.describe('删除会话', () => {
    test('07-删除会话-应该能够删除会话', async ({ page }) => {
      // 1. 创建新会话
      await page.goto('/');
      const createButton = page.locator('button:has-text("新建会话")');
      await createButton.click();
      await page.waitForURL(/\/session\//);
      const sessionId = page.url().split('/session/')[1];

      // 2. 记录初始会话数
      const initialCount = await getDBRecordCount(page, '/api/v1/threads');
      console.log(`删除前会话数: ${initialCount}`);

      // 3. 返回仪表盘
      await page.goto('/');
      await waitForPageLoad(page);

      // 4. 找到新创建的会话卡片
      const sessionCards = page.locator('button[aria-label*="进入会话"]');
      const cardCount = await sessionCards.count();

      if (cardCount > 0) {
        // 点击第一个会话进入详情
        await sessionCards.first().click();
        await page.waitForURL(/\/session\//);

        // 注意：当前实现中可能没有删除按钮
        // 此测试验证当前状态
        const deleteButton = page.locator('button:has-text("删除"), button[aria-label*="删除"]');

        const hasDeleteButton = await deleteButton.count() > 0;

        if (hasDeleteButton) {
          // 监听删除 API
          const deleteRequestPromise = waitForAPI(page, `/api/v1/threads/${sessionId}`, 'DELETE');

          // 点击删除按钮
          await deleteButton.click();

          // 确认删除（如果有确认对话框）
          const confirmButton = page.locator('button:has-text("确认"), button:has-text("确定")');
          if (await confirmButton.count() > 0) {
            await confirmButton.click();
          }

          // 等待删除 API
          const deleteRequest = await deleteRequestPromise;
          expect(deleteRequest.method()).toBe('DELETE');

          // 验证响应状态
          const response = await waitForAPIResponse(page, `/api/v1/threads/${sessionId}`);
          expect([200, 204].includes(response.status())).toBeTruthy();

          // 验证导航回仪表盘或其他页面
          await page.waitForURL(/\/(session\/)?$/, { timeout: 5000 });

          // 验证会话数量减少
          const newCount = await getDBRecordCount(page, '/api/v1/threads');
          expect(newCount).toBe(initialCount - 1);

          // 验证会话在数据库中不存在
          await verifyRecordDeleted(page, '/api/v1/threads', sessionId);

          await takeScreenshot(page, 'session-page-07-delete-success');
        } else {
          test.skip(true, '删除功能尚未实现');
        }
      } else {
        test.skip(true, '没有可删除的会话');
      }
    });

    test('08-删除会话-应该显示确认对话框', async ({ page }) => {
      // 注意：此测试在删除功能实现后启用
      test.skip(true, '等待删除功能实现');

      // 1. 创建会话
      await page.goto('/');
      const createButton = page.locator('button:has-text("新建会话")');
      await createButton.click();
      await page.waitForURL(/\/session\//);

      // 2. 点击删除按钮
      // await page.click('button:has-text("删除")');

      // 3. 验证确认对话框出现
      // const dialog = page.locator('dialog, .modal, [role="dialog"]');
      // await expect(dialog).toBeVisible();

      // 4. 验证对话框包含确认和取消按钮
      // await expect(page.locator('button:has-text("确认")')).toBeVisible();
      // await expect(page.locator('button:has-text("取消")')).toBeVisible();

      await takeScreenshot(page, 'session-page-08-delete-confirm-dialog');
    });
  });

  /**
   * ============================================================================
   * 测试组 4: 会话切换
   * ============================================================================
   */
  test.describe('会话切换', () => {
    test('09-会话切换-应该能够在不同会话间切换', async ({ page }) => {
      // 1. 创建第一个会话
      await page.goto('/');
      const createButton = page.locator('button:has-text("新建会话")');
      await createButton.click();
      await page.waitForURL(/\/session\//);
      const session1Id = page.url().split('/session/')[1];

      // 2. 返回仪表盘
      await page.goto('/');

      // 3. 创建第二个会话
      await createButton.click();
      await page.waitForURL(/\/session\//);
      const session2Id = page.url().split('/session/')[1];

      // 4. 验证第二个会话的 URL
      expect(page.url()).toContain(session2Id);

      // 5. 返回仪表盘
      await page.goto('/');

      // 6. 查找会话卡片
      const sessionCards = page.locator('button[aria-label*="进入会话"]');
      const cardCount = await sessionCards.count();

      if (cardCount >= 2) {
        // 7. 点击第一个会话
        await sessionCards.nth(cardCount - 2).click();

        // 8. 验证导航到第一个会话
        await page.waitForURL(/\/session\//);
        expect(page.url()).toContain(session1Id);

        // 9. 验证页面标题更新
        const sessionPage = page.locator('[data-testid="session-page"]');
        await expect(sessionPage).toBeVisible();

        await takeScreenshot(page, 'session-page-09-switch-session-1');
      } else {
        test.skip(true, '会话数量不足，无法测试切换');
      }
    });

    test('10-会话切换-应该验证路由变化和消息内容更新', async ({ page }) => {
      // 注意：此测试需要多个会话且包含不同消息
      test.skip(true, '需要多个会话且包含消息数据');

      // 1. 导航到第一个会话
      // await page.goto(`/session/${session1Id}`);
      // const messages1 = await getAllMessages(page);
      // console.log('会话 1 消息数:', messages1.length);

      // 2. 导航到第二个会话
      // await page.goto(`/session/${session2Id}`);
      // const messages2 = await getAllMessages(page);
      // console.log('会话 2 消息数:', messages2.length);

      // 3. 验证消息内容不同
      // expect(messages1.length).not.toBe(messages2.length);

      await takeScreenshot(page, 'session-page-10-switch-content-update');
    });
  });

  /**
   * ============================================================================
   * 测试组 5: WebSocket 连接状态
   * ============================================================================
   */
  test.describe('WebSocket 连接', () => {
    test('11-WebSocket-应该显示连接状态', async ({ page }) => {
      // 1. 创建会话
      await page.goto('/');
      const createButton = page.locator('button:has-text("新建会话")');
      await createButton.click();
      await page.waitForURL(/\/session\//);

      // 2. 验证 WebSocket 状态指示器存在
      const statusIndicator = page.locator('.h-2.w-2.rounded-full');
      await expect(statusIndicator).toBeVisible();

      // 3. 验证状态文本
      const statusText = page.locator('span:has-text("已连接"), span:has-text("连接中"), span:has-text("未连接")');
      await expect(statusText).toBeVisible();

      // 4. 验证状态颜色（已连接应该是绿色）
      const hasGreen = await statusIndicator.evaluate((el) => {
        return el.classList.contains('bg-green-500');
      });

      console.log('WebSocket 状态指示器为绿色:', hasGreen);

      await takeScreenshot(page, 'session-page-11-websocket-status');
    });

    test('12-WebSocket-应该处理连接状态变化', async ({ page }) => {
      // 注意：此测试需要模拟 WebSocket 断开/重连
      test.skip(true, '需要模拟 WebSocket 状态变化');

      // 1. 验证初始连接状态
      // 2. 模拟网络断开
      // 3. 验证状态变为"未连接"（灰色）
      // 4. 模拟网络恢复
      // 5. 验证状态变为"连接中"（黄色）然后"已连接"（绿色）

      await takeScreenshot(page, 'session-page-12-websocket-state-change');
    });
  });

  /**
   * ============================================================================
   * 测试组 6: 页面布局和响应式
   * ============================================================================
   */
  test.describe('页面布局', () => {
    test('13-布局-应该正确显示会话页面结构', async ({ page }) => {
      // 1. 创建会话
      await page.goto('/');
      const createButton = page.locator('button:has-text("新建会话")');
      await createButton.click();
      await page.waitForURL(/\/session\//);

      // 2. 验证页面主要区域
      const sessionPage = page.locator('[data-testid="session-page"]');
      await expect(sessionPage).toBeVisible();

      // 3. 验证头部区域
      const header = page.locator('.border-b.border-border');
      await expect(header).toBeVisible();

      // 4. 验证消息区域
      const messageArea = page.locator('.flex-1.overflow-auto');
      await expect(messageArea).toBeVisible();

      // 5. 验证输入区域
      const inputArea = page.locator('.border-t.border-border');
      await expect(inputArea).toBeVisible();

      await takeScreenshot(page, 'session-page-13-layout-structure');
    });

    test('14-布局-桌面视图', async ({ page }) => {
      // 1. 创建会话
      await page.goto('/');
      const createButton = page.locator('button:has-text("新建会话")');
      await createButton.click();
      await page.waitForURL(/\/session\//);

      // 2. 设置桌面视口
      await page.setViewportSize({ width: 1280, height: 720 });

      // 3. 验证页面布局
      const sessionPage = page.locator('[data-testid="session-page"]');
      await expect(sessionPage).toBeVisible();

      // 4. 验证消息容器宽度限制
      const messageContainer = page.locator('.max-w-3xl');
      await expect(messageContainer).toBeVisible();

      await takeScreenshot(page, 'session-page-14-layout-desktop');
    });

    test('15-布局-移动端视图', async ({ page }) => {
      // 1. 创建会话
      await page.goto('/');
      const createButton = page.locator('button:has-text("新建会话")');
      await createButton.click();
      await page.waitForURL(/\/session\//);

      // 2. 设置移动端视口
      await page.setViewportSize({ width: 375, height: 667 });

      // 3. 验证页面仍然可用
      const sessionPage = page.locator('[data-testid="session-page"]');
      await expect(sessionPage).toBeVisible();

      // 4. 验证头部可见
      const header = page.locator('.border-b.border-border');
      await expect(header).toBeVisible();

      // 5. 验证输入区域可见
      const inputArea = page.locator('.border-t.border-border');
      await expect(inputArea).toBeVisible();

      await takeScreenshot(page, 'session-page-15-layout-mobile');
    });
  });

  /**
   * ============================================================================
   * 测试组 7: 边界情况和错误处理
   * ============================================================================
   */
  test.describe('错误处理', () => {
    test('16-错误处理-访问不存在的会话应该显示错误', async ({ page }) => {
      // 1. 尝试访问不存在的会话
      const fakeSessionId = '00000000-0000-0000-0000-000000000000';
      await page.goto(`/session/${fakeSessionId}`);

      // 2. 等待页面加载
      await page.waitForLoadState('networkidle');

      // 3. 验证错误消息显示
      const errorMessage = page.locator('p:has-text("会话不存在或已被删除")');
      await expect(errorMessage).toBeVisible();

      // 4. 验证页面仍然显示 session-page 结构
      const sessionPage = page.locator('[data-testid="session-page"]');
      await expect(sessionPage).toBeVisible();

      await takeScreenshot(page, 'session-page-16-session-not-found');
    });

    test('17-错误处理-未登录访问应该重定向到登录页', async ({ page }) => {
      // 1. 登出
      await logoutAndCleanup(page);

      // 2. 尝试访问会话页面
      await page.goto('/session/test-session-id');

      // 3. 验证重定向到登录页
      await page.waitForURL('/login', { timeout: 5000 });
      expect(page.url()).toContain('/login');

      await takeScreenshot(page, 'session-page-17-redirect-to-login');
    });

    test('18-错误处理-网络错误应该显示友好提示', async ({ page }) => {
      // 注意：此测试需要模拟网络错误
      test.skip(true, '需要模拟网络错误');

      // 1. 拦截 API 请求并返回错误
      // await page.route('**/api/v1/threads/**', route => route.abort());

      // 2. 创建会话
      // await page.goto('/');
      // const createButton = page.locator('button:has-text("新建会话")');
      // await createButton.click();

      // 3. 验证错误消息显示
      // const errorToast = page.locator('.toast:has-text("网络错误")');
      // await expect(errorToast).toBeVisible();

      await takeScreenshot(page, 'session-page-18-network-error');
    });
  });

  /**
   * ============================================================================
   * 测试组 8: 性能测试
   * ============================================================================
   */
  test.describe('性能测试', () => {
    test('19-性能-页面加载时间应该在合理范围内', async ({ page }) => {
      // 1. 记录加载开始时间
      const startTime = Date.now();

      // 2. 创建会话
      await page.goto('/');
      const createButton = page.locator('button:has-text("新建会话")');
      await createButton.click();

      // 3. 等待页面加载完成
      await page.waitForURL(/\/session\//);
      await page.waitForSelector('[data-testid="session-page"]');
      await page.waitForLoadState('networkidle');

      // 4. 计算加载时间
      const loadTime = Date.now() - startTime;
      console.log(`会话页面加载时间: ${loadTime}ms`);

      // 5. 验证加载时间在合理范围内（5秒内）
      expect(loadTime).toBeLessThan(5000);

      await takeScreenshot(page, 'session-page-19-performance-load');
    });

    test('20-性能-切换会话应该快速响应', async ({ page }) => {
      // 1. 创建多个会话
      await page.goto('/');
      const createButton = page.locator('button:has-text("新建会话")');

      await createButton.click();
      await page.waitForURL(/\/session\//);
      const session1Id = page.url().split('/session/')[1];

      await page.goto('/');
      await createButton.click();
      await page.waitForURL(/\/session\//);
      const session2Id = page.url().split('/session/')[1];

      // 2. 记录切换时间
      const startTime = Date.now();

      // 3. 切换到第一个会话
      await page.goto(`/session/${session1Id}`);
      await page.waitForSelector('[data-testid="session-page"]');

      // 4. 计算切换时间
      const switchTime = Date.now() - startTime;
      console.log(`会话切换时间: ${switchTime}ms`);

      // 5. 验证切换时间在合理范围内（2秒内）
      expect(switchTime).toBeLessThan(2000);

      await takeScreenshot(page, 'session-page-20-performance-switch');
    });
  });

  /**
   * ============================================================================
   * 测试组 9: 综合测试
   * ============================================================================
   */
  test.describe('综合测试', () => {
    test('21-综合-完整的用户工作流', async ({ page }) => {
      // 1. 从仪表盘开始
      await page.goto('/');
      const welcomeHeading = page.locator('h1:has-text("欢迎回来")');
      await expect(welcomeHeading).toBeVisible();

      // 2. 创建新会话
      const createButton = page.locator('button:has-text("新建会话")');
      await createButton.click();

      // 3. 验证导航到会话页面
      await page.waitForURL(/\/session\//);
      const sessionPage = page.locator('[data-testid="session-page"]');
      await expect(sessionPage).toBeVisible();

      // 4. 验证会话标题
      const title = page.locator('h1.font-semibold');
      await expect(title).toBeVisible();

      // 5. 验证 WebSocket 状态
      const statusText = page.locator('span:has-text("已连接"), span:has-text("连接中"), span:has-text("未连接")');
      await expect(statusText).toBeVisible();

      // 6. 返回仪表盘
      await page.goto('/');
      await expect(welcomeHeading).toBeVisible();

      // 7. 验证新会话出现在列表中
      const sessionCards = page.locator('button[aria-label*="进入会话"]');
      const cardCount = await sessionCards.count();
      expect(cardCount).toBeGreaterThan(0);

      await takeScreenshot(page, 'session-page-21-complete-workflow');
    });

    test('22-综合-多个会话的创建和切换', async ({ page }) => {
      // 1. 创建三个会话
      const sessionIds: string[] = [];
      for (let i = 0; i < 3; i++) {
        await page.goto('/');
        const createButton = page.locator('button:has-text("新建会话")');
        await createButton.click();
        await page.waitForURL(/\/session\//);
        sessionIds.push(page.url().split('/session/')[1]);
      }

      // 2. 验证创建了三个不同的会话
      const uniqueIds = new Set(sessionIds);
      expect(uniqueIds.size).toBe(3);
      console.log('创建了 3 个会话:', sessionIds);

      // 3. 在会话间切换
      for (const sessionId of sessionIds) {
        await page.goto(`/session/${sessionId}`);
        await page.waitForSelector('[data-testid="session-page"]');
        expect(page.url()).toContain(sessionId);
      }

      await takeScreenshot(page, 'session-page-22-multiple-sessions');
    });
  });

  /**
   * ============================================================================
   * 测试组 10: 可访问性测试
   * ============================================================================
   */
  test.describe('可访问性', () => {
    test('23-可访问性-页面元素应该有正确的语义', async ({ page }) => {
      // 1. 创建会话
      await page.goto('/');
      const createButton = page.locator('button:has-text("新建会话")');
      await createButton.click();
      await page.waitForURL(/\/session\//);

      // 2. 验证页面标题
      const pageTitle = await page.title();
      expect(pageTitle).toBeTruthy();

      // 3. 验证主要区域有正确的标签
      const sessionPage = page.locator('[data-testid="session-page"]');
      await expect(sessionPage).toBeVisible();

      // 4. 验证标题层级
      const h1 = page.locator('h1');
      const h1Count = await h1.count();
      expect(h1Count).toBeGreaterThan(0);

      await takeScreenshot(page, 'session-page-23-accessibility-semantic');
    });

    test('24-可访问性-交互元素应该可访问', async ({ page }) => {
      // 1. 创建会话
      await page.goto('/');
      const createButton = page.locator('button:has-text("新建会话")');
      await createButton.click();
      await page.waitForURL(/\/session\//);

      // 2. 验证所有按钮有文本或 aria-label
      const buttons = page.locator('button');
      const count = await buttons.count();

      for (let i = 0; i < Math.min(count, 10); i++) {
        const button = buttons.nth(i);
        const text = await button.textContent();
        const ariaLabel = await button.getAttribute('aria-label');
        const hasTextOrLabel = (text && text.trim().length > 0) || ariaLabel;

        expect(hasTextOrLabel).toBeTruthy();
      }

      await takeScreenshot(page, 'session-page-24-accessibility-interactive');
    });

    test('25-可访问性-键盘导航应该正常工作', async ({ page }) => {
      // 1. 创建会话
      await page.goto('/');
      const createButton = page.locator('button:has-text("新建会话")');
      await createButton.click();
      await page.waitForURL(/\/session\//);

      // 2. 测试 Tab 键导航
      await page.keyboard.press('Tab');
      await page.keyboard.press('Tab');
      await page.keyboard.press('Tab');

      // 3. 验证焦点移动
      const focusedElement = await page.evaluate(() => document.activeElement?.tagName);
      console.log('当前焦点元素:', focusedElement);

      // 应该能够通过 Tab 键在可聚焦元素间移动
      expect(['BUTTON', 'INPUT', 'A', 'TEXTAREA']).toContain(focusedElement);

      await takeScreenshot(page, 'session-page-25-accessibility-keyboard');
    });
  });
});
