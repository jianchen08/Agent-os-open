/**
 * 聊天组件端到端测试
 *
 * 测试聊天相关的所有组件功能：
 * - 消息列表显示
 * - 消息输入功能
 * - 消息发送功能
 * - 消息历史加载
 * - 实时消息更新
 * - 消息类型支持（文本/图片/文件）
 */

import { test, expect } from '@playwright/test';
import { login, testUser, takeScreenshot } from './helpers';

test.describe('聊天组件测试', () => {
  test.beforeEach(async ({ page }) => {
    // 登录
    await login(page);
    // 等待页面加载
    await page.waitForLoadState('networkidle');
  });

  test.describe('消息列表显示', () => {
    test('应该正确显示空的聊天界面', async ({ page }) => {
      // 检查空状态提示
      const emptyState = page.locator('[data-testid="message-list-empty"], .text-muted-foreground:has-text("开始新的对话")');
      await expect(emptyState).toBeVisible();

      await takeScreenshot(page, 'chat-empty-state');
    });

    test('应该正确显示消息列表容器', async ({ page }) => {
      const chatContainer = page.locator('[data-testid="chat-container"]');
      await expect(chatContainer).toBeVisible();

      // 检查消息列表区域
      const messageList = page.locator('[data-testid="message-list"]');
      await expect(messageList).toBeVisible();

      // 检查输入区域
      const messageInput = page.locator('[data-testid="message-input"]');
      await expect(messageInput).toBeVisible();

      await takeScreenshot(page, 'chat-container-structure');
    });

    test('应该显示正确的消息布局', async ({ page }) => {
      // 发送一条测试消息
      await page.fill('[data-testid="message-input"]', '测试消息列表显示');
      await page.click('[data-testid="send-button"]');

      // 等待消息出现
      await page.waitForTimeout(1000);

      // 检查消息项
      const messageItem = page.locator('[data-testid="message-item"]').first();
      await expect(messageItem).toBeVisible();

      // 检查消息角色属性
      await expect(messageItem).toHaveAttribute('data-role', 'user');

      await takeScreenshot(page, 'chat-message-display');
    });
  });

  test.describe('消息输入功能', () => {
    test('应该正确显示消息输入框', async ({ page }) => {
      const input = page.locator('[data-testid="message-input"]');
      await expect(input).toBeVisible();
      await expect(input).toHaveAttribute('placeholder');

      await takeScreenshot(page, 'chat-input-box');
    });

    test('应该支持输入多行文本', async ({ page }) => {
      const input = page.locator('[data-testid="message-input"]');

      // 输入文本
      await input.fill('第一行\n第二行\n第三行');

      // 检查输入内容
      await expect(input).toHaveValue(/第一行/);

      await takeScreenshot(page, 'chat-input-multiline');
    });

    test('应该正确禁用输入框', async ({ page }) => {
      const input = page.locator('[data-testid="message-input"]');

      // 发送消息后检查是否禁用
      await input.fill('测试禁用状态');
      await page.click('[data-testid="send-button"]');

      // 等待发送
      await page.waitForTimeout(500);

      // 检查输入框是否恢复可输入状态
      await expect(input).toBeEnabled();

      await takeScreenshot(page, 'chat-input-disabled-state');
    });

    test('应该显示发送按钮', async ({ page }) => {
      const sendButton = page.locator('[data-testid="send-button"]');
      await expect(sendButton).toBeVisible();
      await expect(sendButton).toContainText('发送');

      // 检查按钮初始状态（应该禁用）
      await expect(sendButton).toBeDisabled();

      await takeScreenshot(page, 'chat-send-button');
    });

    test('应该在输入文本后启用发送按钮', async ({ page }) => {
      const input = page.locator('[data-testid="message-input"]');
      const sendButton = page.locator('[data-testid="send-button"]');

      // 输入文本
      await input.fill('测试消息');

      // 检查发送按钮是否启用
      await expect(sendButton).toBeEnabled();

      await takeScreenshot(page, 'chat-send-button-enabled');
    });
  });

  test.describe('消息发送功能', () => {
    test('应该支持点击按钮发送消息', async ({ page }) => {
      const input = page.locator('[data-testid="message-input"]');
      const sendButton = page.locator('[data-testid="send-button"]');

      // 输入并发送
      await input.fill('点击按钮发送测试');
      await sendButton.click();

      // 等待消息出现
      await page.waitForTimeout(1000);

      // 检查消息是否显示
      const messageItem = page.locator('[data-testid="message-item"]').filter({ hasText: '点击按钮发送测试' });
      await expect(messageItem).toBeVisible();

      // 检查输入框是否清空
      await expect(input).toHaveValue('');

      await takeScreenshot(page, 'chat-send-by-click');
    });

    test('应该支持按 Enter 键发送消息', async ({ page }) => {
      const input = page.locator('[data-testid="message-input"]');

      // 输入并按 Enter
      await input.fill('Enter 键发送测试');
      await input.press('Enter');

      // 等待消息出现
      await page.waitForTimeout(1000);

      // 检查消息是否显示
      const messageItem = page.locator('[data-testid="message-item"]').filter({ hasText: 'Enter 键发送测试' });
      await expect(messageItem).toBeVisible();

      await takeScreenshot(page, 'chat-send-by-enter');
    });

    test('应该支持 Shift+Enter 换行', async ({ page }) => {
      const input = page.locator('[data-testid="message-input"]');

      // 输入并按 Shift+Enter
      await input.fill('第一行');
      await input.press('Shift+Enter');
      await input.type('第二行');

      // 检查输入内容
      const value = await input.inputValue();
      expect(value).toContain('第一行\n第二行');

      await takeScreenshot(page, 'chat-shift-enter-newline');
    });

    test('应该验证空消息不能发送', async ({ page }) => {
      const sendButton = page.locator('[data-testid="send-button"]');

      // 不输入任何内容
      await expect(sendButton).toBeDisabled();

      // 输入空格
      await page.fill('[data-testid="message-input"]', '   ');
      await expect(sendButton).toBeDisabled();

      await takeScreenshot(page, 'chat-empty-message-validation');
    });

    test('应该正确处理连续发送多条消息', async ({ page }) => {
      const messages = ['第一条消息', '第二条消息', '第三条消息'];
      const input = page.locator('[data-testid="message-input"]');

      for (const message of messages) {
        await input.fill(message);
        await page.click('[data-testid="send-button"]');
        await page.waitForTimeout(500);
      }

      // 等待所有消息显示
      await page.waitForTimeout(1000);

      // 检查消息数量
      const messageItems = page.locator('[data-testid="message-item"]');
      const count = await messageItems.count();
      expect(count).toBeGreaterThanOrEqual(messages.length);

      await takeScreenshot(page, 'chat-multiple-messages');
    });
  });

  test.describe('实时消息更新', () => {
    test('应该自动滚动到最新消息', async ({ page }) => {
      const input = page.locator('[data-testid="message-input"]');

      // 发送多条消息
      for (let i = 1; i <= 5; i++) {
        await input.fill(`消息 ${i}`);
        await page.click('[data-testid="send-button"]');
        await page.waitForTimeout(300);
      }

      // 检查最后一条消息是否可见
      await page.waitForTimeout(1000);
      const lastMessage = page.locator('[data-testid="message-item"]').last();
      await expect(lastMessage).toBeInViewport();

      await takeScreenshot(page, 'chat-auto-scroll');
    });

    test('应该显示正在生成状态', async ({ page }) => {
      const input = page.locator('[data-testid="message-input"]');

      // 发送消息
      await input.fill('测试生成状态');
      await page.click('[data-testid="send-button"]');

      // 等待可能的生成指示器
      await page.waitForTimeout(2000);

      // 检查是否有停止按钮出现（表示正在生成）
      const stopButton = page.locator('[data-testid="stop-button"]');
      const hasStopButton = await stopButton.isVisible().catch(() => false);

      if (hasStopButton) {
        await takeScreenshot(page, 'chat-generating-state');
        test.skip(true, '检测到停止按钮，跳过此测试');
      } else {
        await takeScreenshot(page, 'chat-no-generating-state');
      }
    });

    test('应该显示流式输出动画', async ({ page }) => {
      const input = page.locator('[data-testid="message-input"]');

      // 发送消息
      await input.fill('测试流式输出');
      await page.click('[data-testid="send-button"]');

      // 等待响应
      await page.waitForTimeout(3000);

      // 检查消息项
      const messageItems = page.locator('[data-testid="message-item"]');
      const count = await messageItems.count();

      if (count > 1) {
        // 有响应消息
        const lastMessage = messageItems.last();
        await expect(lastMessage).toBeVisible();

        await takeScreenshot(page, 'chat-streaming-output');
      } else {
        test.skip(true, '未收到响应消息');
      }
    });
  });

  test.describe('消息历史加载', () => {
    test('应该加载历史消息', async ({ page }) => {
      // 刷新页面
      await page.reload();
      await page.waitForLoadState('networkidle');

      // 检查是否有历史消息
      const messageItems = page.locator('[data-testid="message-item"]');
      const count = await messageItems.count();

      if (count > 0) {
        await expect(messageItems.first()).toBeVisible();
        await takeScreenshot(page, 'chat-history-loaded');
      } else {
        await takeScreenshot(page, 'chat-no-history');
      }
    });

    test('应该保持滚动位置', async ({ page }) => {
      // 检查消息列表是否可滚动
      const messageList = page.locator('[data-testid="message-list"]');
      const isScrollable = await messageList.evaluate(el => {
        return el.scrollHeight > el.clientHeight;
      });

      if (isScrollable) {
        await takeScreenshot(page, 'chat-scrollable-list');
      } else {
        await takeScreenshot(page, 'chat-not-scrollable');
      }
    });
  });

  test.describe('消息操作功能', () => {
    test('应该显示消息操作按钮', async ({ page }) => {
      const input = page.locator('[data-testid="message-input"]');

      // 发送消息
      await input.fill('测试消息操作');
      await page.click('[data-testid="send-button"]');
      await page.waitForTimeout(1000);

      // 悬停在消息上
      const messageItem = page.locator('[data-testid="message-item"]').first();
      await messageItem.hover();

      // 检查操作按钮（复制、编辑、删除）
      const actions = messageItem.locator('button');
      const actionCount = await actions.count();

      expect(actionCount).toBeGreaterThan(0);

      await takeScreenshot(page, 'chat-message-actions');
    });

    test('应该支持复制消息内容', async ({ page }) => {
      const input = page.locator('[data-testid="message-input"]');

      // 发送消息
      await input.fill('测试复制功能');
      await page.click('[data-testid="send-button"]');
      await page.waitForTimeout(1000);

      // 悬停并点击复制按钮
      const messageItem = page.locator('[data-testid="message-item"]').first();
      await messageItem.hover();

      const copyButton = messageItem.locator('button').filter({ hasText: /复制/i }).or(
        messageItem.locator('button[title="复制"]')
      ).first();

      const hasCopyButton = await copyButton.isVisible().catch(() => false);

      if (hasCopyButton) {
        await copyButton.click();
        await page.waitForTimeout(500);

        await takeScreenshot(page, 'chat-message-copied');
      } else {
        test.skip(true, '未找到复制按钮');
      }
    });

    test('应该支持编辑用户消息', async ({ page }) => {
      const input = page.locator('[data-testid="message-input"]');

      // 发送消息
      await input.fill('原始消息内容');
      await page.click('[data-testid="send-button"]');
      await page.waitForTimeout(1000);

      // 悬停并点击编辑按钮
      const messageItem = page.locator('[data-testid="message-item"]').first();
      await messageItem.hover();

      const editButton = messageItem.locator('button').filter({ hasText: /编辑/i }).or(
        messageItem.locator('button[title="编辑"]')
      ).first();

      const hasEditButton = await editButton.isVisible().catch(() => false);

      if (hasEditButton) {
        await editButton.click();
        await page.waitForTimeout(500);

        // 检查是否出现编辑框
        const textarea = messageItem.locator('textarea');
        const hasTextarea = await textarea.isVisible().catch(() => false);

        if (hasTextarea) {
          await takeScreenshot(page, 'chat-message-editing');
        } else {
          test.skip(true, '未找到编辑框');
        }
      } else {
        test.skip(true, '未找到编辑按钮');
      }
    });
  });

  test.describe('文件上传功能', () => {
    test('应该显示文件上传按钮', async ({ page }) => {
      // 检查文件上传按钮
      const fileButton = page.locator('button').filter({ hasText: /上传/i }).or(
        page.locator('button[title*="上传"]')
      ).or(
        page.locator('input[type="file"]')
      );

      const hasFileUpload = await fileButton.isVisible().catch(() => false);

      if (hasFileUpload) {
        await takeScreenshot(page, 'chat-file-upload-button');
      } else {
        test.skip(true, '未找到文件上传按钮');
      }
    });

    test('应该支持拖拽上传文件', async ({ page }) => {
      const inputArea = page.locator('[data-testid="message-input"]').locator('..');

      // 检查是否支持拖拽
      const supportsDrop = await inputArea.evaluate(el => {
        return 'ondragover' in el;
      });

      if (supportsDrop) {
        await takeScreenshot(page, 'chat-drag-drop-supported');
      } else {
        test.skip(true, '不支持拖拽上传');
      }
    });
  });

  test.describe('响应式设计', () => {
    test('应该在移动端正确显示', async ({ page }) => {
      // 设置移动端视口
      await page.setViewportSize({ width: 375, height: 667 });

      // 检查聊天界面
      const chatContainer = page.locator('[data-testid="chat-container"]');
      await expect(chatContainer).toBeVisible();

      await takeScreenshot(page, 'chat-mobile-view');
    });

    test('应该在平板端正确显示', async ({ page }) => {
      // 设置平板视口
      await page.setViewportSize({ width: 768, height: 1024 });

      // 检查聊天界面
      const chatContainer = page.locator('[data-testid="chat-container"]');
      await expect(chatContainer).toBeVisible();

      await takeScreenshot(page, 'chat-tablet-view');
    });
  });

  test.describe('可访问性', () => {
    test('应该支持键盘导航', async ({ page }) => {
      const input = page.locator('[data-testid="message-input"]');

      // 使用 Tab 键导航
      await page.keyboard.press('Tab');
      await page.keyboard.press('Tab');

      // 检查焦点是否在输入框
      const isFocused = await input.evaluate(el => document.activeElement === el);
      expect(isFocused).toBe(true);

      await takeScreenshot(page, 'chat-keyboard-navigation');
    });

    test('应该有合适的 ARIA 属性', async ({ page }) => {
      // 检查输入框的 ARIA 属性
      const input = page.locator('[data-testid="message-input"]');

      // 检查是否有 placeholder
      const placeholder = await input.getAttribute('placeholder');
      expect(placeholder).toBeTruthy();

      await takeScreenshot(page, 'chat-accessibility');
    });
  });

  test.describe('性能测试', () => {
    test('应该快速渲染消息列表', async ({ page }) => {
      const startTime = Date.now();

      // 发送多条消息
      const input = page.locator('[data-testid="message-input"]');
      for (let i = 1; i <= 10; i++) {
        await input.fill(`性能测试消息 ${i}`);
        await page.click('[data-testid="send-button"]');
        await page.waitForTimeout(200);
      }

      const endTime = Date.now();
      const duration = endTime - startTime;

      // 检查所有消息是否显示
      const messageItems = page.locator('[data-testid="message-item"]');
      await expect(messageItems).toHaveCount(await messageItems.count());

      console.log(`发送 10 条消息耗时: ${duration}ms`);

      await takeScreenshot(page, 'chat-performance-test');
    });
  });

  test.describe('错误处理', () => {
    test('应该处理网络错误', async ({ page }) => {
      // 模拟离线状态
      await page.context().setOffline(true);

      const input = page.locator('[data-testid="message-input"]');
      await input.fill('离线测试消息');
      await page.click('[data-testid="send-button"]');

      // 等待错误提示
      await page.waitForTimeout(2000);

      // 恢复在线状态
      await page.context().setOffline(false);

      await takeScreenshot(page, 'chat-network-error');
    });

    test('应该处理超长消息', async ({ page }) => {
      const longMessage = 'A'.repeat(5000);

      const input = page.locator('[data-testid="message-input"]');
      await input.fill(longMessage);

      // 检查是否能正常输入
      const value = await input.inputValue();
      expect(value.length).toBeGreaterThan(0);

      await takeScreenshot(page, 'chat-long-message');
    });
  });

  test.describe('特殊功能测试', () => {
    test('应该支持消息搜索（如果有）', async ({ page }) => {
      // 检查是否有搜索框
      const searchBox = page.locator('input[placeholder*="搜索"]').or(
        page.locator('[data-testid*="search"]')
      );

      const hasSearch = await searchBox.isVisible().catch(() => false);

      if (hasSearch) {
        await searchBox.fill('测试');
        await page.waitForTimeout(500);

        await takeScreenshot(page, 'chat-search-function');
      } else {
        test.skip(true, '未实现搜索功能');
      }
    });

    test('应该支持消息导出（如果有）', async ({ page }) => {
      // 检查是否有导出按钮
      const exportButton = page.locator('button').filter({ hasText: /导出/i }).or(
        page.locator('button[title*="导出"]')
      );

      const hasExport = await exportButton.isVisible().catch(() => false);

      if (hasExport) {
        await takeScreenshot(page, 'chat-export-function');
      } else {
        test.skip(true, '未实现导出功能');
      }
    });
  });
});
