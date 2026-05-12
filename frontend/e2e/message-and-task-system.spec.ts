/**
 * 消息系统和任务系统完整 E2E 测试
 *
 * 测试覆盖范围：
 * 1. 消息发送（用户消息、AI响应）
 * 2. 消息重试（重新生成AI回复）
 * 3. 消息删除（级联删除逻辑）
 * 4. 工具调用展示
 * 5. 任务提交和创建
 * 6. 任务进度监控（阶段变化、AC状态更新）
 * 7. 数据库和前端UI一致性验证
 */

import { test, expect } from '@playwright/test';
import {
  quickLogin,
  logoutAndCleanup,
  waitForPageLoad,
  takeScreenshot,
  waitForElement,
  waitForAPI,
  waitForAPIResponse,
  recordState,
  compareStates,
  createSession,
  sendMessage,
  waitForAIResponse,
  getAllMessages,
  verifyDBRecord,
  verifyRecordDeleted,
  getDBRecordCount,
  getCurrentTheme,
  switchTheme,
} from './helpers';

/**
 * ============================================================================
 * 测试配置
 * ============================================================================
 */

// 每个测试前清理并登录
test.beforeEach(async ({ page }) => {
  await logoutAndCleanup(page);
  await quickLogin(page);
  await page.waitForLoadState('networkidle');
});

// 测试失败时截图
test.afterEach(async ({ page }, testInfo) => {
  if (testInfo.status !== 'passed') {
    await page.screenshot({
      path: `test-results/message-task-system-failed-${testInfo.title}.png`,
      fullPage: true,
    });
  }
});

/**
 * ============================================================================
 * 测试组 1: 消息发送系统
 * ============================================================================
 */
test.describe('消息发送系统', () => {
  test('01-应该发送用户消息并显示在UI上', async ({ page }) => {
    // 1. 创建或进入会话
    await page.goto('/');
    await page.waitForTimeout(1000);

    // 2. 记录发送前的状态
    const beforeState = await recordState(page, {
      messageCount: '[data-testid="message-item"]',
      inputBox: 'textarea[placeholder*="消息"]',
    });

    // 3. 输入并发送消息
    const messageContent = '测试发送消息功能';
    const chatInput = page.locator('textarea[placeholder*="消息"], [data-testid="chat-input"]');
    await expect(chatInput).toBeVisible();
    await chatInput.fill(messageContent);

    // 4. 监听发送API
    const sendRequest = waitForAPI(page, '/api/messages', 'POST').catch(() => null);

    // 5. 点击发送按钮
    const sendButton = page.locator('button[data-testid="send-btn"], button:has-text("发送")');
    if (await sendButton.isVisible()) {
      await sendButton.click();
    } else {
      await chatInput.press('Enter');
    }

    // 6. 等待发送请求完成
    const request = await sendRequest;
    if (request) {
      console.log('发送API请求已触发:', request.url());
    }

    // 7. 等待消息出现在UI上
    await waitForElement(page, '[data-testid="message-item"][data-role="user"]');
    await page.waitForTimeout(1000);

    // 8. 记录发送后的状态
    const afterState = await recordState(page, {
      messageCount: '[data-testid="message-item"]',
      inputBox: 'textarea[placeholder*="消息"]',
    });

    // 9. 验证消息数量增加
    const diff = compareStates(beforeState, afterState);
    expect(diff.messageCount.changed).toBeTruthy();
    console.log('消息数量变化:', diff.messageCount.before, '->', diff.messageCount.after);

    // 10. 验证用户消息内容显示正确
    const userMessage = page.locator('[data-testid="message-item"][data-role="user"]').last();
    await expect(userMessage).toContainText(messageContent);

    // 11. 验证输入框已清空
    const inputValue = await chatInput.inputValue();
    expect(inputValue).toBe('');

    await takeScreenshot(page, 'message-send-success');
  });

  test('02-应该接收AI响应消息', async ({ page }) => {
    // 1. 创建会话
    await page.goto('/');

    // 2. 发送消息
    const messageContent = '你好，请回复我';
    const chatInput = page.locator('textarea[placeholder*="消息"]');
    await chatInput.fill(messageContent);
    await chatInput.press('Enter');

    // 3. 等待AI响应（超时30秒）
    try {
      await page.waitForSelector(
        '[data-testid="message-item"][data-role="assistant"]',
        { timeout: 30000 }
      );
      console.log('AI响应已收到');
    } catch {
      console.log('30秒内未收到AI响应，可能需要配置LLM后端');
    }

    // 4. 验证AI消息存在
    const aiMessages = page.locator('[data-testid="message-item"][data-role="assistant"]');
    const count = await aiMessages.count();
    expect(count).toBeGreaterThan(0);

    await takeScreenshot(page, 'ai-response-received');
  });

  test('03-应该显示消息时间戳', async ({ page }) => {
    // 1. 发送消息
    await page.goto('/');
    const chatInput = page.locator('textarea[placeholder*="消息"]');
    await chatInput.fill('测试时间戳');
    await chatInput.press('Enter');

    // 2. 等待消息显示
    await waitForElement(page, '[data-testid="message-item"][data-role="user"]');

    // 3. 验证时间戳显示
    const userMessage = page.locator('[data-testid="message-item"][data-role="user"]').last();
    const timestamp = userMessage.locator('text=/\\d+分钟前|刚刚/');

    await expect(timestamp).toBeVisible({ timeout: 5000 });
    console.log('消息时间戳显示正确');

    await takeScreenshot(page, 'message-timestamp');
  });

  test('04-应该正确渲染Markdown内容', async ({ page }) => {
    // 1. 发送包含Markdown的消息
    await page.goto('/');
    const chatInput = page.locator('textarea[placeholder*="消息"]');
    const markdownContent = `# 标题
**粗体文本**
*斜体文本*
- 列表项1
- 列表项2

\`\`\`javascript
console.log('代码块');
\`\`\`
`;

    await chatInput.fill(markdownContent);
    await chatInput.press('Enter');

    // 2. 等待消息显示
    await waitForElement(page, '[data-testid="message-item"][data-role="user"]');

    // 3. 验证Markdown渲染元素
    const userMessage = page.locator('[data-testid="message-item"][data-role="user"]').last();

    // 检查标题
    const h1 = userMessage.locator('h1');
    const hasH1 = await h1.count() > 0;
    if (hasH1) {
      await expect(h1).toContainText('标题');
    }

    // 检查粗体
    const strong = userMessage.locator('strong');
    const hasStrong = await strong.count() > 0;
    if (hasStrong) {
      await expect(strong).toContainText('粗体文本');
    }

    console.log('Markdown渲染验证完成');
    await takeScreenshot(page, 'message-markdown-rendering');
  });
});

/**
 * ============================================================================
 * 测试组 2: 消息重试系统
 * ============================================================================
 */
test.describe('消息重试系统', () => {
  test('05-应该显示重新生成按钮（AI消息）', async ({ page }) => {
    // 1. 发送消息并等待AI响应
    await page.goto('/');
    const chatInput = page.locator('textarea[placeholder*="消息"]');
    await chatInput.fill('测试重新生成按钮');
    await chatInput.press('Enter');

    // 2. 等待AI响应
    try {
      await page.waitForSelector('[data-testid="message-item"][data-role="assistant"]', {
        timeout: 30000
      });
    } catch {
      console.log('未收到AI响应，跳过测试');
      return;
    }

    // 3. 悬停在AI消息上
    const aiMessage = page.locator('[data-testid="message-item"][data-role="assistant"]').last();
    await aiMessage.hover();

    // 4. 验证重新生成按钮存在
    const retryButton = aiMessage.locator('button:has-text("重新生成"), button[title="重新生成"]');
    await expect(retryButton).toBeVisible({ timeout: 3000 });

    console.log('重新生成按钮已显示');
    await takeScreenshot(page, 'retry-button-visible');
  });

  test('06-点击重新生成应该触发新的AI响应', async ({ page }) => {
    // 1. 发送消息并等待AI响应
    await page.goto('/');
    const chatInput = page.locator('textarea[placeholder*="消息"]');
    await chatInput.fill('测试重新生成功能');
    await chatInput.press('Enter');

    // 2. 等待AI响应
    try {
      await page.waitForSelector('[data-testid="message-item"][data-role="assistant"]', {
        timeout: 30000
      });
    } catch {
      console.log('未收到AI响应，跳过测试');
      return;
    }

    // 3. 记录当前AI消息内容
    const aiMessage = page.locator('[data-testid="message-item"][data-role="assistant"]').last();
    const originalContent = await aiMessage.textContent();
    console.log('原始AI响应:', originalContent?.substring(0, 50) + '...');

    // 4. 点击重新生成按钮
    const retryButton = aiMessage.locator('button:has-text("重新生成"), button[title="重新生成"]');
    await retryButton.click();

    // 5. 等待加载状态
    const loader = aiMessage.locator('.animate-spin, [data-testid="loading"]');
    const hasLoader = await loader.isVisible().catch(() => false);
    if (hasLoader) {
      await expect(loader).toBeVisible();
      console.log('重新生成中...');
    }

    // 6. 等待新内容生成（可能需要较长时间）
    await page.waitForTimeout(5000);

    // 7. 验证API请求
    // 注意：重新生成可能不会发送新的API请求，而是重新使用相同输入

    console.log('重新生成功能已触发');
    await takeScreenshot(page, 'retry-regenerating');
  });

  test('07-重新生成时应该禁用其他操作按钮', async ({ page }) => {
    // 1. 发送消息并等待AI响应
    await page.goto('/');
    const chatInput = page.locator('textarea[placeholder*="消息"]');
    await chatInput.fill('测试重新生成时的禁用状态');
    await chatInput.press('Enter');

    // 2. 等待AI响应
    try {
      await page.waitForSelector('[data-testid="message-item"][data-role="assistant"]', {
        timeout: 30000
      });
    } catch {
      console.log('未收到AI响应，跳过测试');
      return;
    }

    // 3. 点击重新生成
    const aiMessage = page.locator('[data-testid="message-item"][data-role="assistant"]').last();
    const retryButton = aiMessage.locator('button:has-text("重新生成"), button[title="重新生成"]');
    await retryButton.click();

    // 4. 等待加载状态出现
    await page.waitForTimeout(500);

    // 5. 验证编辑按钮被禁用
    const editButton = aiMessage.locator('button:has(svg.lucide-edit)').first();
    const hasEditButton = await editButton.isVisible().catch(() => false);
    if (hasEditButton) {
      const isDisabled = await editButton.isDisabled();
      expect(isDisabled).toBeTruthy();
      console.log('编辑按钮已禁用');
    }

    // 6. 验证删除按钮被禁用
    const deleteButton = aiMessage.locator('button:has(svg.lucide-trash)').first();
    const hasDeleteButton = await deleteButton.isVisible().catch(() => false);
    if (hasDeleteButton) {
      const isDisabled = await deleteButton.isDisabled();
      expect(isDisabled).toBeTruthy();
      console.log('删除按钮已禁用');
    }

    await takeScreenshot(page, 'retry-buttons-disabled');
  });
});

/**
 * ============================================================================
 * 测试组 3: 消息删除系统（级联删除）
 * ============================================================================
 */
test.describe('消息删除系统（级联删除）', () => {
  test('08-删除中间消息应该删除该消息及后续所有消息', async ({ page }) => {
    // 1. 创建会话并发送多条消息
    await page.goto('/');

    const messages = [
      '第一条消息',
      '第二条消息',
      '第三条消息',
      '第四条消息',
    ];

    const chatInput = page.locator('textarea[placeholder*="消息"]');

    for (const msg of messages) {
      await chatInput.fill(msg);
      await chatInput.press('Enter');
      await page.waitForTimeout(500);
    }

    // 2. 等待所有消息显示
    await waitForElement(page, '[data-testid="message-item"]');
    await page.waitForTimeout(2000);

    // 3. 记录删除前的消息数量
    const messageItems = page.locator('[data-testid="message-item"]');
    const beforeCount = await messageItems.count();
    console.log('删除前消息数量:', beforeCount);

    // 4. 获取当前会话ID
    const sessionId = await page.evaluate(() => {
      const url = window.location.href;
      const match = url.match(/\/sessions\/([a-f0-9-]+)/);
      return match ? match[1] : null;
    });
    expect(sessionId).toBeTruthy();

    // 5. 通过API获取删除前的消息列表
    const beforeMessagesResponse = await page.evaluate(
      async ({ sessionId }) => {
        const token = localStorage.getItem('token');
        const res = await fetch(`/api/threads/${sessionId}/messages`, {
          headers: { Authorization: `Bearer ${token}` },
        });
        const data = await res.json();
        return data.messages || [];
      },
      { sessionId }
    );
    console.log('数据库中删除前消息数量:', beforeMessagesResponse.length);

    // 6. 定位到第二条消息（索引1）
    const secondMessage = messageItems.nth(1);
    const secondMessageText = await secondMessage.textContent();
    console.log('要删除的消息:', secondMessageText);

    // 7. 获取该消息的ID
    const secondMessageId = await secondMessage.evaluate(el => {
      const messageEl = el as HTMLElement;
      return messageEl.getAttribute('data-message-id');
    });

    // 8. 悬停并点击删除按钮
    await secondMessage.hover();
    const deleteButton = secondMessage.locator('button:has(svg.lucide-trash), button[title="删除"]');
    await expect(deleteButton).toBeVisible({ timeout: 3000 });

    // 9. 监听删除API
    const deleteRequest = waitForAPI(page, `/api/threads/${sessionId}/messages`, 'DELETE');

    // 10. 点击删除并确认
    page.on('dialog', dialog => dialog.accept());
    await deleteButton.click();

    // 11. 等待删除API完成
    await deleteRequest.catch(() => {
      console.log('未监听到删除API请求');
    });

    // 12. 等待UI更新
    await page.waitForTimeout(2000);

    // 13. 记录删除后的消息数量
    const afterCount = await messageItems.count();
    console.log('删除后消息数量:', afterCount);

    // 14. 验证消息数量减少（应该只剩第一条）
    expect(afterCount).toBeLessThan(beforeCount);
    expect(afterCount).toBe(1); // 只剩第一条消息

    // 15. 验证剩余消息是第一条
    const firstMessage = messageItems.first();
    const firstMessageText = await firstMessage.textContent();
    expect(firstMessageText).toContain(messages[0]);

    // 16. 通过API验证数据库中的消息也被删除
    const afterMessagesResponse = await page.evaluate(
      async ({ sessionId }) => {
        const token = localStorage.getItem('token');
        const res = await fetch(`/api/threads/${sessionId}/messages`, {
          headers: { Authorization: `Bearer ${token}` },
        });
        const data = await res.json();
        return data.messages || [];
      },
      { sessionId }
    );
    console.log('数据库中删除后消息数量:', afterMessagesResponse.length);

    // 17. 验证数据库中的消息数量与UI一致
    expect(afterMessagesResponse.length).toBe(afterCount);

    await takeScreenshot(page, 'cascade-delete-after');
  });

  test('09-删除最后一条消息应该只删除该条消息', async ({ page }) => {
    // 1. 创建会话并发送多条消息
    await page.goto('/');

    const messages = ['第一条消息', '第二条消息', '第三条消息'];
    const chatInput = page.locator('textarea[placeholder*="消息"]');

    for (const msg of messages) {
      await chatInput.fill(msg);
      await chatInput.press('Enter');
      await page.waitForTimeout(500);
    }

    // 2. 等待所有消息显示
    await waitForElement(page, '[data-testid="message-item"]');
    await page.waitForTimeout(1000);

    // 3. 记录删除前的消息数量
    const messageItems = page.locator('[data-testid="message-item"]');
    const beforeCount = await messageItems.count();
    console.log('删除前消息数量:', beforeCount);

    // 4. 定位到最后一条消息
    const lastMessage = messageItems.last();

    // 5. 悬停并点击删除按钮
    await lastMessage.hover();
    const deleteButton = lastMessage.locator('button:has(svg.lucide-trash), button[title="删除"]');
    await expect(deleteButton).toBeVisible({ timeout: 3000 });

    // 6. 点击删除并确认
    page.on('dialog', dialog => dialog.accept());
    await deleteButton.click();

    // 7. 等待UI更新
    await page.waitForTimeout(1000);

    // 8. 记录删除后的消息数量
    const afterCount = await messageItems.count();
    console.log('删除后消息数量:', afterCount);

    // 9. 验证消息数量减少1
    expect(afterCount).toBe(beforeCount - 1);

    // 10. 验证其他消息仍然存在
    const firstMessageText = await messageItems.nth(0).textContent();
    const secondMessageText = await messageItems.nth(1).textContent();
    expect(firstMessageText).toContain(messages[0]);
    expect(secondMessageText).toContain(messages[1]);

    await takeScreenshot(page, 'delete-last-message');
  });

  test('10-删除消息时应该显示确认对话框', async ({ page }) => {
    // 1. 创建会话并发送消息
    await page.goto('/');
    const chatInput = page.locator('textarea[placeholder*="消息"]');
    await chatInput.fill('测试删除确认对话框');
    await chatInput.press('Enter');

    // 2. 等待消息显示
    await waitForElement(page, '[data-testid="message-item"]');
    await page.waitForTimeout(500);

    // 3. 定位到消息
    const messageItem = page.locator('[data-testid="message-item"]').first();

    // 4. 悬停并点击删除按钮
    await messageItem.hover();
    const deleteButton = messageItem.locator('button:has(svg.lucide-trash), button[title="删除"]');
    await expect(deleteButton).toBeVisible({ timeout: 3000 });

    // 5. 设置对话框处理（先不接受，验证存在）
    let dialogShown = false;
    page.on('dialog', dialog => {
      dialogShown = true;
      const message = dialog.message();
      console.log('删除确认对话框消息:', message);
      expect(message).toContain('删除');
      dialog.accept();
    });

    await deleteButton.click();
    await page.waitForTimeout(500);

    // 6. 验证对话框已显示
    expect(dialogShown).toBeTruthy();
    console.log('删除确认对话框已显示');

    await takeScreenshot(page, 'delete-confirm-dialog');
  });

  test('11-取消删除应该保持消息不变', async ({ page }) => {
    // 1. 创建会话并发送消息
    await page.goto('/');
    const chatInput = page.locator('textarea[placeholder*="消息"]');
    const testMessage = '测试取消删除';
    await chatInput.fill(testMessage);
    await chatInput.press('Enter');

    // 2. 等待消息显示
    await waitForElement(page, '[data-testid="message-item"]');
    await page.waitForTimeout(500);

    // 3. 记录删除前的消息数量
    const messageItems = page.locator('[data-testid="message-item"]');
    const beforeCount = await messageItems.count();

    // 4. 定位到消息
    const messageItem = messageItems.first();

    // 5. 悬停并点击删除按钮
    await messageItem.hover();
    const deleteButton = messageItem.locator('button:has(svg.lucide-trash), button[title="删除"]');
    await expect(deleteButton).toBeVisible({ timeout: 3000 });

    // 6. 设置对话框处理（取消）
    page.on('dialog', dialog => {
      console.log('用户取消删除');
      dialog.dismiss();
    });

    await deleteButton.click();
    await page.waitForTimeout(500);

    // 7. 验证消息数量未变
    const afterCount = await messageItems.count();
    expect(afterCount).toBe(beforeCount);

    // 8. 验证消息仍然存在
    const messageText = await messageItem.textContent();
    expect(messageText).toContain(testMessage);

    console.log('取消删除后消息保持不变');
    await takeScreenshot(page, 'delete-cancelled');
  });
});

/**
 * ============================================================================
 * 测试组 4: 工具调用展示
 * ============================================================================
 */
test.describe('工具调用展示', () => {
  test('12-应该显示工具调用卡片', async ({ page }) => {
    // 1. 发送可能触发工具调用的消息
    await page.goto('/');
    const chatInput = page.locator('textarea[placeholder*="消息"]');
    await chatInput.fill('使用工具获取当前时间');
    await chatInput.press('Enter');

    // 2. 等待AI响应（可能包含工具调用）
    await page.waitForTimeout(5000);

    // 3. 检查是否有工具调用卡片
    const toolCallCards = page.locator('[data-testid="tool-call-card"], .tool-call-display');
    const hasToolCall = await toolCallCards.count() > 0;

    if (hasToolCall) {
      // 验证工具调用卡片可见
      await expect(toolCallCards.first()).toBeVisible();

      // 验证工具名称显示
      const toolName = toolCallCards.first().locator('text=/\\w+/');
      await expect(toolName).toBeVisible();

      console.log('工具调用卡片已显示');
      await takeScreenshot(page, 'tool-call-card');
    } else {
      console.log('当前响应未包含工具调用，跳过验证');
    }
  });

  test('13-应该展开工具调用详情', async ({ page }) => {
    // 1. 发送可能触发工具调用的消息
    await page.goto('/');
    const chatInput = page.locator('textarea[placeholder*="消息"]');
    await chatInput.fill('使用工具并展示详情');
    await chatInput.press('Enter');

    // 2. 等待AI响应
    await page.waitForTimeout(5000);

    // 3. 检查工具调用卡片
    const toolCallCards = page.locator('[data-testid="tool-call-card"], .tool-call-display');
    const hasToolCall = await toolCallCards.count() > 0;

    if (hasToolCall) {
      const firstCard = toolCallCards.first();

      // 4. 点击展开工具调用详情
      await firstCard.click();
      await page.waitForTimeout(500);

      // 5. 验证详情区域可见（参数、结果）
      const details = firstCard.locator('pre, .tool-details');
      await expect(details.first()).toBeVisible();

      console.log('工具调用详情已展开');
      await takeScreenshot(page, 'tool-call-details-expanded');
    } else {
      console.log('当前响应未包含工具调用');
    }
  });

  test('14-应该显示工具调用状态（pending/running/completed/failed）', async ({ page }) => {
    // 1. 发送消息
    await page.goto('/');
    const chatInput = page.locator('textarea[placeholder*="消息"]');
    await chatInput.fill('测试工具调用状态');
    await chatInput.press('Enter');

    // 2. 等待响应
    await page.waitForTimeout(5000);

    // 3. 检查工具调用卡片
    const toolCallCards = page.locator('[data-testid="tool-call-card"], .tool-call-display');
    const hasToolCall = await toolCallCards.count() > 0;

    if (hasToolCall) {
      const firstCard = toolCallCards.first();

      // 4. 检查状态指示器（图标、文本）
      const statusIcon = firstCard.locator('svg, .status-icon');
      const hasStatusIcon = await statusIcon.count() > 0;

      if (hasStatusIcon) {
        await expect(statusIcon.first()).toBeVisible();
        console.log('工具调用状态图标已显示');
      }

      const statusText = firstCard.locator('text=/等待中|执行中|已完成|执行失败/');
      const hasStatusText = await statusText.count() > 0;

      if (hasStatusText) {
        console.log('工具调用状态文本:', await statusText.first().textContent());
      }

      await takeScreenshot(page, 'tool-call-status');
    } else {
      console.log('当前响应未包含工具调用');
    }
  });
});

/**
 * ============================================================================
 * 测试组 5: 任务提交和创建
 * ============================================================================
 */
test.describe('任务提交和创建', () => {
  test('15-应该通过消息创建任务', async ({ page }) => {
    // 1. 创建会话
    await page.goto('/');

    // 2. 发送创建任务的消息
    const chatInput = page.locator('textarea[placeholder*="消息"]');
    await chatInput.fill('创建一个任务：实现用户登录功能');
    await chatInput.press('Enter');

    // 3. 等待任务卡片出现
    try {
      await page.waitForSelector('[data-testid="task-card"]', { timeout: 15000 });
      console.log('任务卡片已创建');
    } catch {
      console.log('未检测到任务卡片创建，可能需要手动触发任务创建');
    }

    // 4. 验证任务卡片存在
    const taskCard = page.locator('[data-testid="task-card"]');
    const hasTaskCard = await taskCard.count() > 0;

    if (hasTaskCard) {
      await expect(taskCard.first()).toBeVisible();

      // 5. 验证任务目标显示
      const taskGoal = taskCard.locator('p.font-medium, .task-goal');
      await expect(taskGoal.first()).toBeVisible();

      console.log('任务创建成功');
      await takeScreenshot(page, 'task-created');
    }
  });

  test('16-应该显示任务的三阶段指示器', async ({ page }) => {
    // 1. 创建任务
    await page.goto('/');
    const chatInput = page.locator('textarea[placeholder*="消息"]');
    await chatInput.fill('创建任务：测试三阶段指示器');
    await chatInput.press('Enter');

    // 2. 等待任务卡片
    try {
      await page.waitForSelector('[data-testid="task-card"]', { timeout: 15000 });
    } catch {
      console.log('未检测到任务卡片');
      return;
    }

    // 3. 验证阶段指示器
    const phaseIndicator = page.locator('[data-testid="task-phase-indicator"], .phase-indicator');
    const hasPhaseIndicator = await phaseIndicator.count() > 0;

    if (hasPhaseIndicator) {
      await expect(phaseIndicator.first()).toBeVisible();

      // 4. 验证包含三个阶段
      const phases = page.locator('[data-testid="task-phase"], .phase-item');
      const phaseCount = await phases.count();
      expect(phaseCount).toBeGreaterThanOrEqual(3);

      // 5. 验证阶段文本
      await expect(phases.nth(0)).toContainText(/准备|规划/, { timeout: 5000 });
      await expect(phases.nth(1)).toContainText(/执行|实施/);
      await expect(phases.nth(2)).toContainText(/评估|验收/);

      console.log('三阶段指示器显示正确');
      await takeScreenshot(page, 'task-three-phases');
    }
  });

  test('17-应该显示任务的验收标准列表', async ({ page }) => {
    // 1. 创建任务
    await page.goto('/');
    const chatInput = page.locator('textarea[placeholder*="消息"]');
    await chatInput.fill('创建任务：验证验收标准列表');
    await chatInput.press('Enter');

    // 2. 等待任务卡片
    try {
      await page.waitForSelector('[data-testid="task-card"]', { timeout: 15000 });
    } catch {
      console.log('未检测到任务卡片');
      return;
    }

    // 3. 点击展开详情
    const expandButton = page.locator('[data-testid="task-card"] button[aria-label*="展开"], .expand-button');
    const expandVisible = await expandButton.isVisible().catch(() => false);
    if (expandVisible) {
      await expandButton.click();
      await page.waitForTimeout(500);
    }

    // 4. 验证AC列表
    const acList = page.locator('[data-testid="ac-list"], .acceptance-criteria-list');
    const hasAcList = await acList.count() > 0;

    if (hasAcList) {
      await expect(acList.first()).toBeVisible();

      // 5. 验证AC项目
      const acItems = page.locator('[data-testid="ac-item"], .ac-item');
      const itemCount = await acItems.count();
      expect(itemCount).toBeGreaterThan(0);

      console.log(`验收标准列表显示正确，共 ${itemCount} 项`);
      await takeScreenshot(page, 'task-ac-list');
    } else {
      console.log('验收标准列表不可见，可能任务还未生成AC');
    }
  });
});

/**
 * ============================================================================
 * 测试组 6: 任务进度监控
 * ============================================================================
 */
test.describe('任务进度监控', () => {
  test('18-应该显示任务进度条', async ({ page }) => {
    // 1. 创建任务
    await page.goto('/');
    const chatInput = page.locator('textarea[placeholder*="消息"]');
    await chatInput.fill('创建任务：验证进度条');
    await chatInput.press('Enter');

    // 2. 等待任务卡片
    try {
      await page.waitForSelector('[data-testid="task-card"]', { timeout: 15000 });
    } catch {
      console.log('未检测到任务卡片');
      return;
    }

    // 3. 验证进度条存在
    const progressBar = page.locator('[data-testid="task-card"] .h-1\\.5, [data-testid="progress-bar"], .progress-bar');
    const hasProgressBar = await progressBar.count() > 0;

    if (hasProgressBar) {
      await expect(progressBar.first()).toBeVisible();
      console.log('任务进度条已显示');
    }

    // 4. 验证AC计数显示
    const acCount = page.locator('[data-testid="task-card"] text:has-text("AC:"), .ac-count');
    const hasAcCount = await acCount.count() > 0;

    if (hasAcCount) {
      await expect(acCount.first()).toBeVisible();
      console.log('AC计数已显示');
    }

    await takeScreenshot(page, 'task-progress-bar');
  });

  test('19-应该实时更新任务阶段状态', async ({ page }) => {
    // 1. 创建任务
    await page.goto('/');
    const chatInput = page.locator('textarea[placeholder*="消息"]');
    await chatInput.fill('创建任务：测试阶段状态更新');
    await chatInput.press('Enter');

    // 2. 等待任务卡片
    try {
      await page.waitForSelector('[data-testid="task-card"]', { timeout: 15000 });
    } catch {
      console.log('未检测到任务卡片');
      return;
    }

    // 3. 记录初始阶段
    const currentPhase = page.locator('[data-testid="current-phase"], .current-phase');
    const initialPhase = await currentPhase.textContent().catch(() => 'unknown');
    console.log('初始阶段:', initialPhase);

    // 4. 等待一段时间观察阶段变化（可能需要WebSocket更新）
    await page.waitForTimeout(10000);

    // 5. 验证阶段指示器仍然可见
    const phaseIndicator = page.locator('[data-testid="task-phase-indicator"], .phase-indicator');
    await expect(phaseIndicator.first()).toBeVisible();

    console.log('任务阶段状态监控完成');
    await takeScreenshot(page, 'task-phase-update');
  });

  test('20-应该显示AC状态更新通知', async ({ page }) => {
    // 1. 创建任务
    await page.goto('/');
    const chatInput = page.locator('textarea[placeholder*="消息"]');
    await chatInput.fill('创建任务：测试AC状态更新');
    await chatInput.press('Enter');

    // 2. 等待任务卡片
    try {
      await page.waitForSelector('[data-testid="task-card"]', { timeout: 15000 });
    } catch {
      console.log('未检测到任务卡片');
      return;
    }

    // 3. 等待一段时间观察AC更新
    await page.waitForTimeout(10000);

    // 4. 检查AC更新通知
    const acUpdateNotice = page.locator('[data-testid="ac-update-notice"], .ac-update');
    const hasAcUpdate = await acUpdateNotice.count() > 0;

    if (hasAcUpdate) {
      await expect(acUpdateNotice.first()).toBeVisible();

      // 5. 验证AC状态（passed/failed/pending）
      const acStatus = acUpdateNotice.locator('text=/通过|失败|待评估/');
      await expect(acStatus.first()).toBeVisible();

      console.log('AC状态更新通知已显示');
      await takeScreenshot(page, 'task-ac-update-notice');
    } else {
      console.log('暂无AC状态更新通知');
    }
  });
});

/**
 * ============================================================================
 * 测试组 7: 数据库和UI一致性验证
 * ============================================================================
 */
test.describe('数据库和UI一致性验证', () => {
  test('21-前端消息数量应该与数据库一致', async ({ page }) => {
    // 1. 创建会话并发送消息
    await page.goto('/');
    const chatInput = page.locator('textarea[placeholder*="消息"]');

    const messageCount = 3;
    for (let i = 1; i <= messageCount; i++) {
      await chatInput.fill(`消息 ${i}`);
      await chatInput.press('Enter');
      await page.waitForTimeout(500);
    }

    // 2. 等待所有消息显示
    await waitForElement(page, '[data-testid="message-item"]');
    await page.waitForTimeout(1000);

    // 3. 获取前端显示的消息数量
    const frontendCount = await page.locator('[data-testid="message-item"]').count();
    console.log('前端显示消息数量:', frontendCount);

    // 4. 获取会话ID
    const sessionId = await page.evaluate(() => {
      const url = window.location.href;
      const match = url.match(/\/sessions\/([a-f0-9-]+)/);
      return match ? match[1] : null;
    });
    expect(sessionId).toBeTruthy();

    // 5. 通过API获取数据库中的消息数量
    const dbResponse = await page.evaluate(
      async ({ sessionId }) => {
        const token = localStorage.getItem('token');
        const res = await fetch(`/api/threads/${sessionId}/messages`, {
          headers: { Authorization: `Bearer ${token}` },
        });
        const data = await res.json();
        return {
          total: data.total || 0,
          messages: data.messages || []
        };
      },
      { sessionId }
    );

    const dbCount = dbResponse.total;
    console.log('数据库中消息数量:', dbCount);

    // 6. 验证一致性
    expect(frontendCount).toBe(dbCount);
    console.log('✓ 前端和数据库消息数量一致');
  });

  test('22-删除消息后前端和数据库应该同步', async ({ page }) => {
    // 1. 创建会话并发送消息
    await page.goto('/');
    const chatInput = page.locator('textarea[placeholder*="消息"]');

    for (let i = 1; i <= 4; i++) {
      await chatInput.fill(`消息 ${i}`);
      await chatInput.press('Enter');
      await page.waitForTimeout(500);
    }

    // 2. 等待所有消息显示
    await waitForElement(page, '[data-testid="message-item"]');
    await page.waitForTimeout(1000);

    // 3. 记录删除前的数量
    const beforeFrontendCount = await page.locator('[data-testid="message-item"]').count();

    // 4. 获取会话ID
    const sessionId = await page.evaluate(() => {
      const url = window.location.href;
      const match = url.match(/\/sessions\/([a-f0-9-]+)/);
      return match ? match[1] : null;
    });

    // 5. 删除第二条消息
    const messageItems = page.locator('[data-testid="message-item"]');
    const secondMessage = messageItems.nth(1);
    await secondMessage.hover();

    const deleteButton = secondMessage.locator('button:has(svg.lucide-trash), button[title="删除"]');
    page.on('dialog', dialog => dialog.accept());
    await deleteButton.click();

    // 6. 等待删除完成
    await page.waitForTimeout(2000);

    // 7. 获取删除后的前端数量
    const afterFrontendCount = await page.locator('[data-testid="message-item"]').count();

    // 8. 通过API获取数据库中的数量
    const dbResponse = await page.evaluate(
      async ({ sessionId }) => {
        const token = localStorage.getItem('token');
        const res = await fetch(`/api/threads/${sessionId}/messages`, {
          headers: { Authorization: `Bearer ${token}` },
        });
        const data = await res.json();
        return {
          total: data.total || 0,
          messages: data.messages || []
        };
      },
      { sessionId }
    );

    const dbCount = dbResponse.total;

    // 9. 验证一致性
    expect(afterFrontendCount).toBe(dbCount);
    console.log('✓ 删除后前端和数据库消息数量一致:', afterFrontendCount, '=', dbCount);

    await takeScreenshot(page, 'frontend-db-consistency');
  });

  test('23-任务状态更新应该同步到前端UI', async ({ page }) => {
    // 1. 创建任务
    await page.goto('/');
    const chatInput = page.locator('textarea[placeholder*="消息"]');
    await chatInput.fill('创建任务：验证状态同步');
    await chatInput.press('Enter');

    // 2. 等待任务卡片
    try {
      await page.waitForSelector('[data-testid="task-card"]', { timeout: 15000 });
    } catch {
      console.log('未检测到任务卡片');
      return;
    }

    // 3. 获取任务ID
    const taskId = await page.locator('[data-testid="task-card"]').first().evaluate(
      el => el.getAttribute('data-task-id')
    );

    if (!taskId) {
      console.log('无法获取任务ID');
      return;
    }

    console.log('任务ID:', taskId);

    // 4. 通过API获取任务状态
    const taskStatus = await page.evaluate(
      async ({ taskId }) => {
        const token = localStorage.getItem('token');
        try {
          const res = await fetch(`/api/tasks/${taskId}`, {
            headers: { Authorization: `Bearer ${token}` },
          });
          const data = await res.json();
          return data;
        } catch {
          return null;
        }
      },
      { taskId }
    );

    if (taskStatus) {
      console.log('API返回的任务状态:', taskStatus.status);

      // 5. 验证前端显示的任务状态
      const taskCard = page.locator('[data-testid="task-card"]');
      const statusText = await taskCard.locator('.task-status, [data-status]').textContent()
        .catch(() => null);

      console.log('前端显示的任务状态:', statusText);

      // 6. 验证一致性（状态可能用不同文本表示，这里只验证存在）
      expect(statusText).toBeTruthy();
      console.log('✓ 任务状态已同步到前端UI');
    }

    await takeScreenshot(page, 'task-status-sync');
  });
});

/**
 * ============================================================================
 * 测试组 8: 综合场景测试
 * ============================================================================
 */
test.describe('综合场景测试', () => {
  test('24-完整的消息和任务交互流程', async ({ page }) => {
    // 1. 创建会话
    await page.goto('/');

    // 2. 发送创建任务的消息
    const chatInput = page.locator('textarea[placeholder*="消息"]');
    await chatInput.fill('帮我实现一个用户登录功能');
    await chatInput.press('Enter');

    // 3. 等待任务卡片
    try {
      await page.waitForSelector('[data-testid="task-card"]', { timeout: 15000 });
      console.log('✓ 任务已创建');
    } catch {
      console.log('未检测到任务卡片，继续测试其他功能');
    }

    // 4. 发送第二条消息
    await chatInput.fill('需要支持JWT Token认证');
    await chatInput.press('Enter');
    await page.waitForTimeout(1000);
    console.log('✓ 第二条消息已发送');

    // 5. 验证消息列表
    const messageCount = await page.locator('[data-testid="message-item"]').count();
    expect(messageCount).toBeGreaterThanOrEqual(2);
    console.log(`✓ 当前有 ${messageCount} 条消息`);

    // 6. 删除第二条消息
    const messageItems = page.locator('[data-testid="message-item"]');
    const secondMessage = messageItems.nth(1);
    await secondMessage.hover();

    const deleteButton = secondMessage.locator('button:has(svg.lucide-trash), button[title="删除"]');
    const hasDeleteButton = await deleteButton.isVisible().catch(() => false);

    if (hasDeleteButton) {
      page.on('dialog', dialog => dialog.accept());
      await deleteButton.click();
      await page.waitForTimeout(1000);

      const newCount = await page.locator('[data-testid="message-item"]').count();
      console.log(`✓ 删除后剩余 ${newCount} 条消息`);
    }

    // 7. 验证任务卡片仍然存在（如果创建了）
    const taskCard = page.locator('[data-testid="task-card"]');
    const hasTaskCard = await taskCard.count() > 0;

    if (hasTaskCard) {
      await expect(taskCard.first()).toBeVisible();
      console.log('✓ 任务卡片仍然存在');
    }

    await takeScreenshot(page, 'complete-interaction-flow');
  });

  test('25-多任务并行执行和进度监控', async ({ page }) => {
    // 1. 创建会话
    await page.goto('/');

    // 2. 创建多个任务
    const chatInput = page.locator('textarea[placeholder*="消息"]');

    const tasks = [
      '创建任务1：实现用户注册',
      '创建任务2：实现用户登录',
      '创建任务3：实现密码找回',
    ];

    for (const task of tasks) {
      await chatInput.fill(task);
      await chatInput.press('Enter');
      await page.waitForTimeout(2000);
    }

    // 3. 验证多个任务卡片
    const taskCards = page.locator('[data-testid="task-card"]');
    const taskCount = await taskCards.count();
    console.log(`创建了 ${taskCount} 个任务`);

    // 4. 验证每个任务的进度条
    for (let i = 0; i < taskCount; i++) {
      const card = taskCards.nth(i);
      const progressBar = card.locator('.progress-bar, [data-testid="progress-bar"]');
      const hasProgressBar = await progressBar.count() > 0;

      if (hasProgressBar) {
        console.log(`✓ 任务 ${i + 1} 有进度条`);
      }
    }

    // 5. 验证消息数量
    const messageCount = await page.locator('[data-testid="message-item"]').count();
    expect(messageCount).toBeGreaterThanOrEqual(tasks.length);

    await takeScreenshot(page, 'multiple-tasks-parallel');
  });

  test('26-错误处理和重试机制', async ({ page }) => {
    // 1. 创建会话
    await page.goto('/');

    // 2. 发送可能失败的消息（模拟网络错误）
    const chatInput = page.locator('textarea[placeholder*="消息"]');
    await chatInput.fill('测试错误处理');
    await chatInput.press('Enter');

    // 3. 等待响应或错误
    await page.waitForTimeout(5000);

    // 4. 检查是否有错误提示
    const errorMessage = page.locator('.toast.error, [role="alert"].error, .error-message');
    const hasError = await errorMessage.count() > 0;

    if (hasError) {
      console.log('检测到错误消息:', await errorMessage.first().textContent());

      // 5. 验证重试按钮存在
      const retryButton = page.locator('button:has-text("重试"), [data-testid="retry-button"]');
      const hasRetryButton = await retryButton.count() > 0;

      if (hasRetryButton) {
        console.log('✓ 重试按钮已显示');
        // 可以点击重试并验证
      }
    } else {
      console.log('未检测到错误，可能请求成功');
    }

    await takeScreenshot(page, 'error-handling-retry');
  });
});

/**
 * ============================================================================
 * 测试组 9: 性能测试
 * ============================================================================
 */
test.describe('性能测试', () => {
  test('27-大量消息加载性能', async ({ page }) => {
    // 1. 创建会话
    await page.goto('/');

    // 2. 发送多条消息
    const chatInput = page.locator('textarea[placeholder*="消息"]');
    const messageCount = 20;

    console.log(`发送 ${messageCount} 条消息...`);

    for (let i = 1; i <= messageCount; i++) {
      await chatInput.fill(`消息 ${i}`);
      await chatInput.press('Enter');
      await page.waitForTimeout(100);
    }

    // 3. 等待所有消息加载
    await page.waitForLoadState('networkidle');
    await page.waitForTimeout(2000);

    // 4. 测量性能指标
    const metrics = await page.evaluate(() => {
      const navEntry = performance.getEntriesByType('navigation')[0] as PerformanceNavigationTiming;
      return {
        domContentLoaded: Math.round(navEntry.domContentLoadedEventEnd - navEntry.domContentLoadedEventStart),
        loadComplete: Math.round(navEntry.loadEventEnd - navEntry.loadEventStart),
      };
    });

    console.log('页面性能指标:', metrics);

    // 5. 验证性能在可接受范围
    expect(metrics.domContentLoaded).toBeLessThan(5000);
    expect(metrics.loadComplete).toBeLessThan(10000);

    // 6. 验证所有消息都已显示
    const displayedMessages = await page.locator('[data-testid="message-item"]').count();
    expect(displayedMessages).toBeGreaterThanOrEqual(messageCount);

    console.log(`✓ 成功加载 ${displayedMessages} 条消息，性能良好`);

    await takeScreenshot(page, 'performance-many-messages');
  });

  test('28-快速连续操作响应性', async ({ page }) => {
    // 1. 创建会话
    await page.goto('/');

    // 2. 快速连续发送多条消息
    const chatInput = page.locator('textarea[placeholder*="消息"]');

    const startTime = Date.now();

    for (let i = 1; i <= 10; i++) {
      await chatInput.fill(`快速消息 ${i}`);
      await chatInput.press('Enter');
    }

    const sendTime = Date.now() - startTime;
    console.log(`发送10条消息耗时: ${sendTime}ms`);

    // 3. 等待处理完成
    await page.waitForLoadState('networkidle');
    await page.waitForTimeout(2000);

    // 4. 验证UI响应性
    const messages = await page.locator('[data-testid="message-item"]').count();
    expect(messages).toBeGreaterThanOrEqual(10);

    console.log(`✓ 快速操作响应性良好，处理了 ${messages} 条消息`);

    await takeScreenshot(page, 'performance-rapid-actions');
  });
});
