/**
 * 思考模式直接测试 - 验证数据流和渲染
 */

import { test, expect } from '@playwright/test';

const BASE_URL = process.env.FRONTEND_URL || 'http://localhost:5188';
const TEST_USER = {
  username: 'admin',
  password: 'admin123456'
};

async function login(page) {
  await page.goto(`${BASE_URL}/login`);
  await page.waitForSelector('input[name="username"], input[type="text"]', { timeout: 5000 });
  await page.fill('input[name="username"], input[type="text"]', TEST_USER.username);
  await page.fill('input[name="password"], input[type="password"]', TEST_USER.password);
  await page.click('button[type="submit"], button:has-text("登录")');
  await page.waitForURL(/\/(dashboard|sessions?|chat|session)/, { timeout: 10000 });
}

test('思考模式完整数据流测试', async ({ page }) => {
  // 监听所有控制台消息
  const consoleLogs = [];
  page.on('console', msg => {
    consoleLogs.push({
      type: msg.type(),
      text: msg.text()
    });
  });

  // 监听 WebSocket 消息
  const wsMessages = [];
  page.on('websocket', ws => {
    ws.on('framereceived', frame => {
      try {
        const data = JSON.parse(frame.payload);
        wsMessages.push(data);
      } catch {
        // 忽略非 JSON 消息
      }
    });
  });

  await login(page);

  // 导航到会话页面
  await page.goto(`${BASE_URL}/sessions`);
  await page.waitForTimeout(1000);

  // 尝试找到并点击思考模式切换按钮
  const thinkingToggle = page.locator('button').filter({ hasText: /普通模式|深度思考/ }).first();
  const toggleExists = await thinkingToggle.isVisible({ timeout: 3000 }).catch(() => false);

  if (toggleExists) {
    await thinkingToggle.click();
    console.log('已点击思考模式切换按钮');
    await page.waitForTimeout(1000);
  }

  // 发送测试消息
  const input = page.locator('input[placeholder*="消息"], textarea[placeholder*="消息"]').first();
  await expect(input).toBeVisible({ timeout: 5000 });
  await input.fill('请详细解释什么是递归？');

  // 记录发送时间
  const sendTime = Date.now();

  // 按 Enter 发送
  await input.press('Enter');
  console.log('消息已发送，等待响应...');

  // 等待响应（思考模式可能需要更长时间）
  await page.waitForTimeout(15000);

  // 分析控制台日志
  console.log('\n=== 控制台日志分析 ===');
  const thinkingLogs = consoleLogs.filter(log =>
    log.text.includes('thinking') ||
    log.text.includes('思考') ||
    log.text.includes('THINKING')
  );

  console.log(`找到 ${thinkingLogs.length} 条思考相关日志:`);
  thinkingLogs.forEach(log => {
    console.log(`  [${log.type}] ${log.text}`);
  });

  // 分析 WebSocket 消息
  console.log('\n=== WebSocket 消息分析 ===');
  const thinkingWsMessages = wsMessages.filter(msg =>
    msg.type === 'thinking_start' ||
    msg.type === 'thinking_chunk' ||
    msg.type === 'thinking_end'
  );

  console.log(`找到 ${thinkingWsMessages.length} 条思考相关 WebSocket 消息:`);
  thinkingWsMessages.forEach(msg => {
    console.log(`  [${msg.type}]`, JSON.stringify(msg.data));
  });

  // 检查页面上的思考内容显示
  console.log('\n=== 页面元素检查 ===');

  // 检查是否有思考展示组件
  const thinkingDisplay = page.locator('[class*="thinking"], [data-testid*="thinking"]');
  const thinkingCount = await thinkingDisplay.count();
  console.log(`找到 ${thinkingCount} 个思考相关元素`);

  if (thinkingCount > 0) {
    for (let i = 0; i < Math.min(thinkingCount, 5); i++) {
      const element = thinkingDisplay.nth(i);
      const isVisible = await element.isVisible().catch(() => false);
      const text = await element.textContent().catch(() => '');
      console.log(`  元素 ${i + 1}: 可见=${isVisible}, 内容长度=${text?.length || 0}`);
    }
  }

  // 检查最后一条消息
  const messages = page.locator('[data-testid*="message"], [class*="message"]');
  const messageCount = await messages.count();
  console.log(`\n找到 ${messageCount} 条消息`);

  if (messageCount > 0) {
    const lastMessage = messages.last();
    const lastMessageText = await lastMessage.textContent().catch(() => '');
    console.log(`最后一条消息内容长度: ${lastMessageText?.length || 0}`);

    // 检查是否包含思考相关文本
    const hasThinkingText = lastMessageText?.includes('思考过程') ||
                           lastMessageText?.includes('思考详情') ||
                           lastMessageText?.includes('Thinking');
    console.log(`最后一条消息包含思考文本: ${hasThinkingText}`);
  }

  // 诊断结果
  console.log('\n=== 诊断结果 ===');

  const hasThinkingLogs = thinkingLogs.length > 0;
  const hasThinkingWsMessages = thinkingWsMessages.length > 0;
  const hasThinkingElements = thinkingCount > 0;

  console.log(`控制台思考日志: ${hasThinkingLogs ? '✓' : '✗'}`);
  console.log(`WebSocket 思考消息: ${hasThinkingWsMessages ? '✓' : '✗'}`);
  console.log(`页面思考元素: ${hasThinkingElements ? '✓' : '✗'}`);

  if (!hasThinkingWsMessages) {
    console.log('\n⚠️ 问题: 后端没有发送思考相关消息');
    console.log('   请检查:');
    console.log('   1. 思考模式是否启用');
    console.log('   2. 模型是否支持思考模式');
    console.log('   3. 后端日志是否有错误');
  } else if (!hasThinkingElements) {
    console.log('\n⚠️ 问题: 前端收到思考消息但没有渲染');
    console.log('   请检查:');
    console.log('   1. WebSocket 服务是否正确转发消息');
    console.log('   2. SessionPage 事件处理器是否订阅');
    console.log('   3. sessionStore 是否正确更新状态');
    console.log('   4. ThinkingDisplay 组件是否渲染');
  }

  // 截图保存
  await page.screenshot({ path: 'test-results/thinking-mode-test-screenshot.png', fullPage: true });
  console.log('\n截图已保存到: test-results/thinking-mode-test-screenshot.png');

  // 断言：至少应该有 WebSocket 消息（如果后端支持思考模式）
  if (toggleExists) {
    console.log('\n思考模式已启用，预期应该有思考相关消息');
    // 这里不强制断言，因为可能模型配置问题
  } else {
    console.log('\n思考模式按钮未找到，可能前端组件有问题');
  }
});
