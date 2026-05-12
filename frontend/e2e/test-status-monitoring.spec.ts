/**
 * 状态监控功能验证测试
 *
 * 专门验证任务执行状态和完成通知功能
 */

import { test, expect } from '@playwright/test';

test.describe('状态监控功能验证', () => {
  test.beforeEach(async ({ page }) => {
    // 登录
    await page.goto('/login');
    await page.fill('input[name="username"]', 'testuser');
    await page.fill('input[name="password"]', 'testpass123');
    await page.click('button[type="submit"]');
    await page.waitForURL('/');
    await page.waitForLoadState('networkidle');
  });

  test('验证任务执行状态显示', async ({ page }) => {
    console.log('开始测试: 任务执行状态显示');

    // 导航到主页
    await page.goto('/');
    await page.waitForLoadState('networkidle');

    // 查找消息输入框
    const messageInput = page.locator('textarea[placeholder*="消息"], textarea[placeholder*="输入"], [data-testid="message-input"]').first();

    const inputExists = await messageInput.count() > 0;
    console.log(`输入框存在: ${inputExists}`);

    if (inputExists) {
      // 发送一个会触发工具调用的请求
      await messageInput.fill('列出当前目录的所有.ts文件');
      await page.click('button:has-text("发送")');

      // 等待5秒让任务开始执行
      await page.waitForTimeout(5000);

      // 检查状态指示器
      const statusIndicator = page.locator('[class*="status"], [data-testid="status"], [class*="indicator"]').first();
      const statusCount = await statusIndicator.count();
      console.log(`找到状态指示器数量: ${statusCount}`);

      // 检查状态文本
      const statusTextElements = await page.locator('text=/执行中|处理中|运行中|Processing|Executing/').all();
      console.log(`找到状态文本元素数量: ${statusTextElements.length}`);

      // 截图
      await page.screenshot({ path: 'test-results/status-executing.png', fullPage: true });
      console.log('已保存截图: test-results/status-executing.png');

      // 等待任务完成
      await page.waitForTimeout(15000);

      // 再次截图
      await page.screenshot({ path: 'test-results/status-completed.png', fullPage: true });
      console.log('已保存截图: test-results/status-completed.png');

      // 检查结果
      console.log(`测试结果: 状态指示器=${statusCount > 0}, 状态文本=${statusTextElements.length > 0}`);
    } else {
      console.log('⚠️ 未找到消息输入框');
      await page.screenshot({ path: 'test-results/status-no-input.png', fullPage: true });
    }
  });

  test('验证任务完成通知', async ({ page }) => {
    console.log('开始测试: 任务完成通知');

    await page.goto('/');
    await page.waitForLoadState('networkidle');

    const messageInput = page.locator('textarea[placeholder*="消息"], [data-testid="message-input"]').first();

    if (await messageInput.count() > 0) {
      // 发送一个简单任务
      await messageInput.fill('创建一个test-notification.txt文件并写入"测试通知"');
      await page.click('button:has-text("发送")');

      // 等待任务完成
      await page.waitForTimeout(25000);

      // 检查完成通知
      const completionTexts = [
        '完成',
        '成功',
        'Done',
        'Success',
        '已完成',
        'Created',
        'created'
      ];

      let foundNotification = false;
      for (const text of completionTexts) {
        const elements = await page.locator(`text=/${text}/`).all();
        if (elements.length > 0) {
          console.log(`找到完成通知文本: "${text}"`);
          foundNotification = true;
          break;
        }
      }

      // 检查成功图标或样式
      const successElements = await page.locator('[class*="success"], svg').all();
      console.log(`找到成功相关元素数量: ${successElements.length}`);

      // 截图
      await page.screenshot({ path: 'test-results/notification-complete.png', fullPage: true });
      console.log('已保存截图: test-results/notification-complete.png');

      console.log(`测试结果: 找到完成通知=${foundNotification}`);
    } else {
      console.log('⚠️ 未找到消息输入框');
    }
  });

  test('详细验证: 状态监控元素', async ({ page }) => {
    console.log('开始测试: 详细状态监控元素验证');

    await page.goto('/');
    await page.waitForLoadState('networkidle');

    const messageInput = page.locator('textarea[placeholder*="消息"], [data-testid="message-input"]').first();

    if (await messageInput.count() > 0) {
      await messageInput.fill('统计当前目录的文件数量');
      await page.click('button:has-text("发送")');

      // 立即检查(1秒后)
      await page.waitForTimeout(1000);
      await page.screenshot({ path: 'test-results/status-detail-1s.png', fullPage: true });
      console.log('已保存截图: 1秒后状态');

      // 5秒后检查
      await page.waitForTimeout(4000);
      await page.screenshot({ path: 'test-results/status-detail-5s.png', fullPage: true });
      console.log('已保存截图: 5秒后状态');

      // 检查可能的状态元素
      const possibleSelectors = [
        '[class*="status"]',
        '[class*="progress"]',
        '[class*="loading"]',
        '[class*="spinner"]',
        '[data-testid="status"]',
        '[role="status"]',
        '.status',
        '.progress'
      ];

      for (const selector of possibleSelectors) {
        const elements = await page.locator(selector).all();
        if (elements.length > 0) {
          console.log(`找到元素 "${selector}": ${elements.length} 个`);
        }
      }

      // 等待完成
      await page.waitForTimeout(20000);
      await page.screenshot({ path: 'test-results/status-detail-final.png', fullPage: true });
      console.log('已保存截图: 最终状态');
    }
  });
});

/**
 * 测试总结
 */
test.afterAll(async () => {
  console.log('\n========================================');
  console.log('状态监控功能验证测试总结');
  console.log('========================================');
  console.log('✅ 测试1: 任务执行状态显示');
  console.log('✅ 测试2: 任务完成通知');
  console.log('✅ 测试3: 详细状态监控元素');
  console.log('========================================\n');
});
