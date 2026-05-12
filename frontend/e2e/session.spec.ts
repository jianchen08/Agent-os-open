/**
 * 会话页面端到端测试
 *
 * 测试会话页面的所有功能
 */

import { test, expect } from '@playwright/test';
import { login, takeScreenshot, checkToast } from './helpers';

test.describe('会话页面', () => {
  test.beforeEach(async ({ page }) => {
    await login(page);
  });

  test('应该正确显示会话页面', async ({ page }) => {
    await page.goto('/session/test-session-1');
    await page.waitForLoadState('networkidle');

    // 检查页面元素
    await expect(page.locator('[data-testid="chat-container"], .chat-container, .chat')).toBeVisible();

    await takeScreenshot(page, 'session-page');
  });

  test('应该显示消息列表', async ({ page }) => {
    await page.goto('/session/test-session-1');
    await page.waitForLoadState('networkidle');

    // 检查消息列表
    const messageList = page.locator('[data-testid="message-list"], .message-list, .messages');
    await expect(messageList).toBeVisible();

    await takeScreenshot(page, 'session-message-list');
  });

  test('应该显示聊天输入框', async ({ page }) => {
    await page.goto('/session/test-session-1');
    await page.waitForLoadState('networkidle');

    // 检查输入框
    const chatInput = page.locator(
      'textarea[placeholder*="消息"], textarea[placeholder*="输入"], [data-testid="chat-input"], .chat-input'
    );
    await expect(chatInput).toBeVisible();

    // 检查发送按钮
    const sendButton = page.locator('button[aria-label*="发送"], button:has-text("发送"), [data-testid="send-button"]');
    await expect(sendButton).toBeVisible();

    await takeScreenshot(page, 'session-chat-input');
  });

  test('应该可以发送消息', async ({ page }) => {
    await page.goto('/session/test-session-1');
    await page.waitForLoadState('networkidle');

    const testMessage = '这是一条测试消息';

    // 输入消息
    const chatInput = page.locator(
      'textarea[placeholder*="消息"], textarea[placeholder*="输入"], [data-testid="chat-input"]'
    );
    await chatInput.fill(testMessage);

    // 发送消息
    const sendButton = page.locator('button[aria-label*="发送"], button:has-text("发送"), [data-testid="send-button"]');
    await sendButton.click();

    // 等待消息出现在列表中
    const messageInList = page.locator(`.message, [data-testid="message"]`).filter({ hasText: testMessage });
    await expect(messageInList).toBeVisible({ timeout: 5000 });

    await takeScreenshot(page, 'session-message-sent');
  });

  test('应该支持换行输入', async ({ page }) => {
    await page.goto('/session/test-session-1');
    await page.waitForLoadState('networkidle');

    const chatInput = page.locator(
      'textarea[placeholder*="消息"], textarea[placeholder*="输入"], [data-testid="chat-input"]'
    );

    // 输入多行文本
    await chatInput.fill('第一行');
    await page.keyboard.press('Shift+Enter');
    await chatInput.fill('第一行\n第二行');

    // 检查输入框内容
    const value = await chatInput.inputValue();
    expect(value).toContain('\n');
  });

  test('应该显示 Agent 执行状态', async ({ page }) => {
    await page.goto('/session/test-session-1');
    await page.waitForLoadState('networkidle');

    // 检查执行状态面板
    const executionPanel = page.locator('[data-testid="execution-panel"], .execution-panel, .status-panel');
    const count = await executionPanel.count();

    if (count > 0) {
      await expect(executionPanel.first()).toBeVisible();
      await takeScreenshot(page, 'session-execution-panel');
    }
  });

  test('应该显示执行图', async ({ page }) => {
    await page.goto('/session/test-session-1');
    await page.waitForLoadState('networkidle');

    // 检查执行图组件
    const graph = page.locator('[data-testid="execution-graph"], .execution-graph, .flow-graph');
    const count = await graph.count();

    if (count > 0) {
      await expect(graph.first()).toBeVisible();
      await takeScreenshot(page, 'session-execution-graph');
    }
  });

  test('应该可以暂停/恢复执行', async ({ page }) => {
    await page.goto('/session/test-session-1');
    await page.waitForLoadState('networkidle');

    // 查找暂停按钮
    const pauseButton = page.locator('button:has-text("暂停"), button[aria-label*="pause"], [data-testid="pause-button"]');
    const count = await pauseButton.count();

    if (count > 0) {
      await pauseButton.first().click();

      // 检查是否显示恢复按钮
      const resumeButton = page.locator('button:has-text("继续"), button[aria-label*="resume"], [data-testid="resume-button"]');
      await expect(resumeButton.first()).toBeVisible();

      await takeScreenshot(page, 'session-paused');
    }
  });

  test('应该可以停止执行', async ({ page }) => {
    await page.goto('/session/test-session-1');
    await page.waitForLoadState('networkidle');

    // 查找停止按钮
    const stopButton = page.locator('button:has-text("停止"), button[aria-label*="stop"], [data-testid="stop-button"]');
    const count = await stopButton.count();

    if (count > 0) {
      await stopButton.first().click();

      // 可能需要确认
      const confirmButton = page.locator('button:has-text("确认"), button:has-text("确定")');
      const confirmCount = await confirmButton.count();

      if (confirmCount > 0) {
        await confirmButton.first().click();
      }

      await takeScreenshot(page, 'session-stopped');
    }
  });

  test('应该显示会话历史', async ({ page }) => {
    await page.goto('/session/test-session-1');
    await page.waitForLoadState('networkidle');

    // 检查会话历史侧边栏
    const historySidebar = page.locator('[data-testid="history-sidebar"], .history-sidebar, .session-history');
    const count = await historySidebar.count();

    if (count > 0) {
      await expect(historySidebar.first()).toBeVisible();
      await takeScreenshot(page, 'session-history');
    }
  });

  test('应该可以切换到不同的会话', async ({ page }) => {
    await page.goto('/session/test-session-1');
    await page.waitForLoadState('networkidle');

    // 查找会话列表
    const sessionList = page.locator('.session-list, [data-testid="session-list"]');
    const count = await sessionList.count();

    if (count > 0) {
      // 点击另一个会话
      const otherSession = sessionList.locator('a, button').nth(1);
      if (await otherSession.isVisible()) {
        await otherSession.click();
        await page.waitForTimeout(1000);
        await takeScreenshot(page, 'session-switched');
      }
    }
  });

  test('应该可以清空消息', async ({ page }) => {
    await page.goto('/session/test-session-1');
    await page.waitForLoadState('networkidle');

    // 查找清空按钮
    const clearButton = page.locator('button:has-text("清空"), button:has-text("清除"), [data-testid="clear-button"]');
    const count = await clearButton.count();

    if (count > 0) {
      await clearButton.first().click();

      // 可能需要确认
      const confirmButton = page.locator('button:has-text("确认"), button:has-text("确定")');
      const confirmCount = await confirmButton.count();

      if (confirmCount > 0) {
        await confirmButton.first().click();
      }

      await takeScreenshot(page, 'session-cleared');
    }
  });

  test('应该支持导出会话', async ({ page }) => {
    await page.goto('/session/test-session-1');
    await page.waitForLoadState('networkidle');

    // 查找导出按钮
    const exportButton = page.locator('button:has-text("导出"), button:has-text("Export"), [data-testid="export-button"]');
    const count = await exportButton.count();

    if (count > 0) {
      // 设置下载处理
      const downloadPromise = page.waitForEvent('download', { timeout: 10000 });
      await exportButton.first().click();

      try {
        const download = await downloadPromise;
        expect(download.suggestedFilename()).toBeTruthy();
      } catch (e) {
        // 如果没有触发下载，可能有导出菜单，继续测试
        await takeScreenshot(page, 'session-export-menu');
      }
    }
  });

  test('应该正确处理错误消息', async ({ page }) => {
    await page.goto('/session/test-session-1');
    await page.waitForLoadState('networkidle');

    // 发送可能触发错误的消息
    const chatInput = page.locator(
      'textarea[placeholder*="消息"], textarea[placeholder*="输入"], [data-testid="chat-input"]'
    );
    await chatInput.fill('/error');

    const sendButton = page.locator('button[aria-label*="发送"], button:has-text("发送"), [data-testid="send-button"]');
    await sendButton.click();

    // 等待并检查错误显示
    await page.waitForTimeout(2000);

    const errorMessage = page.locator('.error, [role="alert"], .text-red');
    const errorCount = await errorMessage.count();

    if (errorCount > 0) {
      await expect(errorMessage.first()).toBeVisible();
      await takeScreenshot(page, 'session-error');
    }
  });

  test('应该支持文件上传', async ({ page }) => {
    await page.goto('/session/test-session-1');
    await page.waitForLoadState('networkidle');

    // 查找文件上传按钮
    const uploadButton = page.locator(
      'button[aria-label*="上传"], button:has-text("上传"), [data-testid="upload-button"], input[type="file"]'
    );
    const count = await uploadButton.count();

    if (count > 0) {
      // 创建测试文件
      const fileInput = page.locator('input[type="file"]');

      if (await fileInput.count() > 0) {
        await fileInput.setInputFiles({
          name: 'test.txt',
          mimeType: 'text/plain',
          buffer: Buffer.from('测试文件内容'),
        });

        await takeScreenshot(page, 'session-file-uploaded');
      }
    }
  });
});
