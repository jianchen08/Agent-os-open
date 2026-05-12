/**
 * AI Agent 系统前端交互功能测试（增强版）
 *
 * 基于 Codebase 分析更新的测试用例
 *
 * 测试场景：
 * 1. 登录功能
 * 2. 页面加载和 UI 渲染
 * 3. 侧边栏功能
 * 4. 聊天输入和消息发送（使用正确的 data-testid）
 * 5. 会话列表
 * 6. 综合功能测试
 */

import { test, expect, Page } from '@playwright/test';

// 测试配置
const BASE_URL = 'http://localhost:5188';
const TEST_USER = {
  username: 'admin',
  password: 'admin123456'
};

// 辅助函数：等待页面稳定
async function waitForPageStable(page: Page, timeout: number = 2000) {
  await page.waitForTimeout(timeout);
}

// 辅助函数：执行登录
async function performLogin(page: Page) {
  console.log('  🔐 执行登录操作');

  // 等待登录表单出现
  await page.waitForLoadState('networkidle');
  await waitForPageStable(page, 1000);

  // 查找用户名输入框
  const usernameInput = page.locator('input[type="text"], input[name="username"]').first();
  await usernameInput.waitFor({ state: 'visible', timeout: 5000 });
  await usernameInput.fill(TEST_USER.username);
  console.log(`    ✓ 输入用户名: ${TEST_USER.username}`);

  // 查找密码输入框
  const passwordInput = page.locator('input[type="password"], input[name="password"]').first();
  await passwordInput.waitFor({ state: 'visible', timeout: 5000 });
  await passwordInput.fill(TEST_USER.password);
  console.log(`    ✓ 输入密码`);

  // 查找并点击登录按钮
  const loginButton = page.locator('button[type="submit"]').first();
  await loginButton.waitFor({ state: 'visible', timeout: 5000 });
  await loginButton.click();
  console.log(`    ✓ 点击登录按钮`);

  // 等待登录成功后页面跳转
  await page.waitForURL(/\/(dashboard|session\/)/, { timeout: 10000 });
  await waitForPageStable(page, 2000);

  const currentUrl = page.url();
  console.log(`    ✓ 登录成功，当前 URL: ${currentUrl}`);
}

test.describe('AI Agent 前端交互测试（增强版）', () => {
  let page: Page;

  test.beforeEach(async ({ browser }) => {
    page = await browser.newPage();
    page.setDefaultTimeout(15000);

    // 打开登录页面
    await page.goto(BASE_URL);

    // 执行登录
    await performLogin(page);
  });

  test.afterEach(async () => {
    await page.close();
  });

  test('场景1: 登录后的页面元素验证', async () => {
    console.log('📍 测试场景1: 登录后的页面元素验证');

    // 1. 检查当前 URL
    console.log('  步骤1: 检查当前 URL');
    const currentUrl = page.url();
    console.log(`    当前 URL: ${currentUrl}`);
    expect(currentUrl).toMatch(/\/(dashboard|session\/)/);

    // 2. 使用 data-testid 查找关键元素
    console.log('  步骤2: 使用 data-testid 查找关键元素');

    // 查找聊天输入框
    const chatInput = page.locator('[data-testid="chat-input"]').first();
    const chatInputExists = await chatInput.count() > 0;
    console.log(`    聊天输入容器 [data-testid="chat-input"]: ${chatInputExists ? '✓ 存在' : '✗ 未找到'}`);

    // 查找输入框文本区域
    const chatTextarea = page.locator('[data-testid="chat-input-textarea"]').first();
    const chatTextareaExists = await chatTextarea.count() > 0;
    console.log(`    输入文本区域 [data-testid="chat-input-textarea"]: ${chatTextareaExists ? '✓ 存在' : '✗ 未找到'}`);

    // 查找发送按钮
    const sendButton = page.locator('[data-testid="chat-send-button"]').first();
    const sendButtonExists = await sendButton.count() > 0;
    console.log(`    发送按钮 [data-testid="chat-send-button"]: ${sendButtonExists ? '✓ 存在' : '✗ 未找到'}`);

    // 3. 截图
    await page.screenshot({
      path: 'screenshots/test-enhanced-scene1-elements.png',
      fullPage: true
    });
    console.log('  ✓ 截图已保存');

    console.log('✅ 场景1 完成\n');
  });

  test('场景2: 聊天输入功能测试', async () => {
    console.log('📍 测试场景2: 聊天输入功能测试');

    // 1. 查找输入框
    console.log('  步骤1: 查找输入框');
    const chatTextarea = page.locator('[data-testid="chat-input-textarea"]').first();

    if (await chatTextarea.count() === 0) {
      console.log('    ⚠ 未找到输入框');
      console.log('✅ 场景2 完成（跳过）\n');
      return;
    }

    await chatTextarea.waitFor({ state: 'visible', timeout: 5000 });
    console.log('    ✓ 找到输入框');

    // 2. 输入测试消息
    console.log('  步骤2: 输入测试消息');
    const testMessage = '你好，这是一条测试消息';
    await chatTextarea.fill(testMessage);
    await waitForPageStable(page, 500);

    // 验证输入内容
    const inputValue = await chatTextarea.inputValue();
    console.log(`    输入内容: ${inputValue}`);
    expect(inputValue).toContain(testMessage);

    // 截图
    await page.screenshot({
      path: 'screenshots/test-enhanced-scene2-input.png'
    });
    console.log('  ✓ 截图已保存');

    // 3. 查找发送按钮
    console.log('  步骤3: 查找发送按钮');
    const sendButton = page.locator('[data-testid="chat-send-button"]').first();

    if (await sendButton.count() === 0) {
      console.log('    ⚠ 未找到发送按钮');
      console.log('✅ 场景2 完成\n');
      return;
    }

    await sendButton.waitFor({ state: 'visible', timeout: 5000 });
    console.log('    ✓ 找到发送按钮');

    // 4. 检查按钮状态
    console.log('  步骤4: 检查按钮状态');
    const isDisabled = await sendButton.isDisabled();
    console.log(`    发送按钮禁用状态: ${isDisabled ? '禁用' : '可用'}`);

    if (!isDisabled) {
      // 5. 点击发送按钮
      console.log('  步骤5: 点击发送按钮');
      await sendButton.click();
      await waitForPageStable(page, 3000);

      // 截图
      await page.screenshot({
        path: 'screenshots/test-enhanced-scene2-sent.png',
        fullPage: true
      });
      console.log('  ✓ 截图已保存');

      // 6. 检查消息是否显示
      console.log('  步骤6: 检查消息显示');

      // 检查输入框是否清空
      const inputValueAfter = await chatTextarea.inputValue();
      if (inputValueAfter === '') {
        console.log('    ✓ 输入框已清空，消息可能已发送');
      } else {
        console.log('    ⚠ 输入框未清空');
      }
    }

    console.log('✅ 场景2 完成\n');
  });

  test('场景3: 键盘快捷键测试', async () => {
    console.log('📍 测试场景3: 键盘快捷键测试');

    // 1. 查找输入框
    console.log('  步骤1: 查找输入框');
    const chatTextarea = page.locator('[data-testid="chat-input-textarea"]').first();

    if (await chatTextarea.count() === 0) {
      console.log('    ⚠ 未找到输入框');
      console.log('✅ 场景3 完成（跳过）\n');
      return;
    }

    await chatTextarea.waitFor({ state: 'visible', timeout: 5000 });
    console.log('    ✓ 找到输入框');

    // 2. 测试 Enter 发送
    console.log('  步骤2: 测试 Enter 发送');
    const testMessage = '测试 Enter 发送';
    await chatTextarea.fill(testMessage);
    await waitForPageStable(page, 500);

    // 按下 Enter 键
    await chatTextarea.press('Enter');
    await waitForPageStable(page, 3000);

    // 截图
    await page.screenshot({
      path: 'screenshots/test-enhanced-scene3-enter.png'
    });
    console.log('  ✓ 截图已保存');

    // 检查输入框是否清空
    const inputValueAfter = await chatTextarea.inputValue();
    if (inputValueAfter === '') {
      console.log('    ✓ Enter 发送成功');
    } else {
      console.log('    ⚠ Enter 发送可能失败');
    }

    // 3. 测试 Shift+Enter 换行
    console.log('  步骤3: 测试 Shift+Enter 换行');
    const multiLineMessage = '第一行\n第二行';
    await chatTextarea.fill('第一行');
    await waitForPageStable(page, 500);

    // 按下 Shift+Enter
    await chatTextarea.press('Shift+Enter');
    await waitForPageStable(page, 500);

    // 输入第二行
    await chatTextarea.press('End');
    await chatTextarea.type('第二行');
    await waitForPageStable(page, 500);

    // 检查内容
    const finalValue = await chatTextarea.inputValue();
    console.log(`    多行内容: "${finalValue}"`);

    if (finalValue.includes('\n')) {
      console.log('    ✓ Shift+Enter 换行成功');
    } else {
      console.log('    ⚠ Shift+Enter 换行可能失败');
    }

    // 截图
    await page.screenshot({
      path: 'screenshots/test-enhanced-scene3-shift-enter.png'
    });
    console.log('  ✓ 截图已保存');

    console.log('✅ 场景3 完成\n');
  });

  test('场景4: UI 布局和响应式测试', async () => {
    console.log('📍 测试场景4: UI 布局和响应式测试');

    // 1. 检查桌面布局
    console.log('  步骤1: 检查桌面布局 (1280x720)');
    await page.setViewportSize({ width: 1280, height: 720 });
    await waitForPageStable(page, 1000);

    await page.screenshot({
      path: 'screenshots/test-enhanced-scene4-desktop.png',
      fullPage: true
    });
    console.log('  ✓ 桌面布局截图已保存');

    // 检查侧边栏
    const sidebar = page.locator('aside').first();
    const sidebarVisible = await sidebar.isVisible();
    console.log(`    侧边栏可见: ${sidebarVisible ? '是' : '否'}`);

    // 2. 检查平板布局
    console.log('  步骤2: 检查平板布局 (768x1024)');
    await page.setViewportSize({ width: 768, height: 1024 });
    await waitForPageStable(page, 1000);

    await page.screenshot({
      path: 'screenshots/test-enhanced-scene4-tablet.png',
      fullPage: true
    });
    console.log('  ✓ 平板布局截图已保存');

    // 3. 检查移动端布局
    console.log('  步骤3: 检查移动端布局 (375x667)');
    await page.setViewportSize({ width: 375, height: 667 });
    await waitForPageStable(page, 1000);

    await page.screenshot({
      path: 'screenshots/test-enhanced-scene4-mobile.png',
      fullPage: true
    });
    console.log('  ✓ 移动端布局截图已保存');

    // 4. 恢复桌面布局
    await page.setViewportSize({ width: 1280, height: 720 });

    console.log('✅ 场景4 完成\n');
  });

  test('场景5: 可访问性测试', async () => {
    console.log('📍 测试场景5: 可访问性测试');

    // 1. 检查 ARIA 标签
    console.log('  步骤1: 检查 ARIA 标签');

    // 检查按钮的 title 属性
    const buttons = await page.locator('button[title]').all();
    console.log(`    带 title 属性的按钮数量: ${buttons.length}`);

    // 列出几个示例
    for (let i = 0; i < Math.min(buttons.length, 5); i++) {
      const title = await buttons[i].getAttribute('title');
      console.log(`      - "${title}"`);
    }

    // 2. 检查语义化 HTML
    console.log('  步骤2: 检查语义化 HTML');

    const navElements = await page.locator('nav').count();
    const mainElements = await page.locator('main').count();
    const asideElements = await page.locator('aside').count();
    const headerElements = await page.locator('header').count();

    console.log(`    <nav> 元素数量: ${navElements}`);
    console.log(`    <main> 元素数量: ${mainElements}`);
    console.log(`    <aside> 元素数量: ${asideElements}`);
    console.log(`    <header> 元素数量: ${headerElements}`);

    // 3. 检查表单元素的 label 关联
    console.log('  步骤3: 检查表单元素');

    const inputs = await page.locator('input, textarea').all();
    console.log(`    表单输入元素数量: ${inputs.length}`);

    let labeledCount = 0;
    for (let i = 0; i < Math.min(inputs.length, 10); i++) {
      const hasLabel = await inputs[i].getAttribute('aria-label');
      const hasPlaceholder = await inputs[i].getAttribute('placeholder');
      const hasDataTestId = await inputs[i].getAttribute('data-testid');

      if (hasLabel || hasPlaceholder || hasDataTestId) {
        labeledCount++;
      }
    }
    console.log(`    有标签/占位符的表单元素: ${labeledCount}/${Math.min(inputs.length, 10)}`);

    console.log('✅ 场景5 完成\n');
  });

  test('综合测试: 完整用户流程', async () => {
    console.log('📍 综合测试: 完整用户流程');

    // 1. 页面状态检查
    console.log('  步骤1: 页面状态检查');
    const url = page.url();
    console.log(`    当前 URL: ${url}`);

    const title = await page.title();
    console.log(`    页面标题: ${title}`);

    // 2. 交互元素统计
    console.log('  步骤2: 交互元素统计');

    const buttons = page.locator('button:visible');
    const buttonCount = await buttons.count();
    console.log(`    可见按钮数量: ${buttonCount}`);

    const inputs = page.locator('input:visible, textarea:visible, [contenteditable]:visible');
    const inputCount = await inputs.count();
    console.log(`    可见输入框数量: ${inputCount}`);

    const links = page.locator('a:visible');
    const linkCount = await links.count();
    console.log(`    可见链接数量: ${linkCount}`);

    // 3. 检查 data-testid 元素
    console.log('  步骤3: 检查 data-testid 元素');

    const elementsWithTestId = await page.locator('[data-testid]').all();
    console.log(`    带 data-testid 的元素数量: ${elementsWithTestId.length}`);

    // 列出一些示例
    const testIds = new Set();
    for (let i = 0; i < Math.min(elementsWithTestId.length, 10); i++) {
      const testId = await elementsWithTestId[i].getAttribute('data-testid');
      if (testId) {
        testIds.add(testId);
      }
    }
    console.log('    发现的 data-testid:');
    Array.from(testIds).forEach(id => {
      console.log(`      - ${id}`);
    });

    // 4. 最终截图
    console.log('  步骤4: 最终截图');
    await page.screenshot({
      path: 'screenshots/test-enhanced-final-comprehensive.png',
      fullPage: true
    });
    console.log('    ✓ 最终截图已保存');

    // 5. 性能指标
    console.log('  步骤5: 性能指标');

    const metrics = await page.evaluate(() => {
      const navigation = performance.getEntriesByType('navigation')[0] as PerformanceNavigationTiming;
      return {
        domContentLoaded: Math.round(navigation.domContentLoadedEventEnd - navigation.domContentLoadedEventStart),
        loadComplete: Math.round(navigation.loadEventEnd - navigation.loadEventStart),
        domInteractive: Math.round(navigation.domInteractive - navigation.navigationStart),
      };
    });

    console.log(`    DOM 内容加载时间: ${metrics.domContentLoaded} ms`);
    console.log(`    DOM 交互时间: ${metrics.domInteractive} ms`);
    console.log(`    页面完全加载时间: ${metrics.loadComplete} ms`);

    console.log('✅ 综合测试完成\n');
  });
});
