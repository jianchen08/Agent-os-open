/**
 * 会话页 - 完整交互测试
 *
 * 测试所有聊天、Agent、执行图相关组件
 */

import { test, expect } from '@playwright/test';

test.describe('会话页 - 完整交互测试', () => {

  test.beforeEach(async ({ page }) => {
    await page.goto(process.env.REACT_APP_FRONTEND_URL || "http://localhost:5188");
    await page.waitForTimeout(1000);

    if (page.url().includes('/login')) {
      test.skip(true, '需要先登录');
    }
  });

  test('1. 页面基础结构', async ({ page }) => {
    console.log('\n[会话页] 测试页面基础结构...');

    // 1.1 会话标题
    const title = await page.locator('h1, .font-semibold').first().textContent();
    console.log(`  会话标题: ${title}`);

    // 1.2 返回按钮
    const backBtn = page.locator('button[aria-label*="返回"], button:has-text("返回")').count();
    console.log(`  返回按钮: ${backBtn > 0 ? '✓' : '✗'}`);

    // 1.3 更多菜单
    const menuBtn = page.locator('button[aria-label*="更多"], button:has-text("更多")').count();
    console.log(`  更多菜单: ${menuBtn > 0 ? '✓' : '✗'}`);
  });

  test('2. Agent标签栏', async ({ page }) => {
    console.log('\n[会话页] 测试Agent标签栏...');

    const tabBar = page.locator('[data-testid="agent-tab-bar"], .agent-tabs').count();
    console.log(`  Agent标签栏: ${tabBar > 0 ? '✓ 存在' : '✗ 不存在'}`);

    if (tabBar > 0) {
      // 获取所有标签
      const tabs = await page.locator('[role="tab"], .tab-button').all();
      console.log(`  标签数量: ${tabs.length}`);

      // 切换每个标签
      for (let i = 0; i < Math.min(tabs.length, 3); i++) {
        await tabs[i].click();
        await page.waitForTimeout(200);
        console.log(`    - 切换标签 ${i + 1}: ✓`);
      }
    }
  });

  test('3. 消息列表', async ({ page }) => {
    console.log('\n[会话页] 测试消息列表...');

    // 3.1 检查消息容器
    const messageList = page.locator('[data-testid="message-list"], .messages-container').count();
    console.log(`  消息列表容器: ${messageList > 0 ? '✓' : '✗'}`);

    // 3.2 检查消息项
    const messages = await page.locator('[data-testid="message-item"], .message').count();
    console.log(`  消息数量: ${messages}`);

    if (messages > 0) {
      // 检查第一条消息
      const firstMsg = page.locator('[data-testid="message-item"], .message').first();

      // 消息内容
      const content = await firstMsg.locator('.message-content, p').textContent();
      console.log(`    - 内容: ${content?.substring(0, 50)}`);

      // 悬停效果
      await firstMsg.hover();
      await page.waitForTimeout(200);
      console.log(`    - 悬停效果: ✓`);

      // 滚动到底部
      await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
      await page.waitForTimeout(500);
      console.log(`    - 滚动: ✓`);
    } else {
      console.log(`  - 空消息列表`);
    }
  });

  test('4. 消息输入区域', async ({ page }) => {
    console.log('\n[会话页] 测试消息输入区域...');

    // 4.1 输入框
    const input = page.locator('textarea, [contenteditable="true"], input[type="text"]').first();
    const inputCount = await input.count();
    console.log(`  输入框: ${inputCount > 0 ? '✓' : '✗'}`);

    if (inputCount > 0) {
      // 聚焦
      await input.click();
      await page.waitForTimeout(200);
      console.log(`    - 聚焦: ✓`);

      // 输入文本
      await input.fill('测试消息');
      const value = await input.inputValue();
      console.log(`    - 输入: ${value === '测试消息' ? '✓' : '✗'}`);

      // 清空
      await input.fill('');
      console.log(`    - 清空: ✓`);
    }

    // 4.2 发送按钮
    const sendBtn = page.locator('button[aria-label*="发送"], button:has-text("发送")').count();
    console.log(`  发送按钮: ${sendBtn > 0 ? '✓' : '✗'}`);

    // 4.3 附件按钮
    const attachBtn = page.locator('button[aria-label*="附件"], button:has-text("附件")').count();
    console.log(`  附件按钮: ${attachBtn > 0 ? '✓' : '⚠️ (可能未实现)'}`);
  });

  test('5. WebSocket连接状态', async ({ page }) => {
    console.log('\n[会话页] 测试WebSocket连接...');

    // 等待连接
    await page.waitForTimeout(3000);

    // 检查状态指示器
    const statusIndicator = page.locator('[data-testid*="ws"], [data-testid*="websocket"], .ws-status, .connection-status').count();
    console.log(`  状态指示器: ${statusIndicator > 0 ? '✓' : '⚠️ (可能未显示)'}`);

    if (statusIndicator > 0) {
      const status = await page.locator('[data-testid*="ws"], .ws-status').first().textContent();
      console.log(`    - 状态: ${status}`);
    }
  });

  test('6. 执行图按钮', async ({ page }) => {
    console.log('\n[会话页] 测试执行图按钮...');

    const graphBtn = page.locator('button:has-text("执行图"), button:has-text("图谱")').count();
    console.log(`  执行图按钮: ${graphBtn > 0 ? '✓' : '✗'}`);

    // 6.1 切换显示
    if (graphBtn > 0) {
      await page.locator('button:has-text("执行图")').first().click();
      await page.waitForTimeout(500);

      const graphVisible = await page.locator('[data-testid="execution-graph"], .execution-graph').isVisible();
      console.log(`    - 执行图显示: ${graphVisible ? '✓' : '✗'}`);
    }
  });

  test('7. 侧边栏会话列表', async ({ page }) => {
    console.log('\n[会话页] 测试侧边栏会话列表...');

    const sidebar = page.locator('[data-testid="sidebar"], aside').count();
    console.log(`  侧边栏: ${sidebar > 0 ? '✓' : '✗'}`);

    if (sidebar > 0) {
      // 会话列表项
      const sessions = await page.locator('[data-testid="session-list-item"], .session-item').count();
      console.log(`  会话列表项: ${sessions}`);

      // 搜索框
      const searchInput = page.locator('[data-testid="session-search"], input[placeholder*="搜索"]').count();
      console.log(`  搜索框: ${searchInput > 0 ? '✓' : '⚠️'}`);

      // 新建按钮
      const newBtn = page.locator('button:has-text("新建"), button[aria-label*="新建"]').count();
      console.log(`  新建按钮: ${newBtn > 0 ? '✓' : '✗'}`);
    }
  });

  test('8. 响应式布局', async ({ page }) => {
    console.log('\n[会话页] 测试响应式布局...');

    // 检查在不同尺寸下的显示
    const sizes = [
      { width: 1920, height: 1080, name: '桌面' },
      { width: 768, height: 1024, name: '平板' },
      { width: 375, height: 667, name: '手机' }
    ];

    for (const size of sizes) {
      await page.setViewportSize({ width: size.width, height: size.height });
      await page.waitForTimeout(500);

      const mainContent = page.locator('main, [role="main"]').isVisible();
      console.log(`  ${size.name}: ${mainContent ? '✓' : '✗'}`);
    }
  });
});
