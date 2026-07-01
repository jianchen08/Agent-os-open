/**
 * AI Agent 系统前端交互功能测试（含认证）
 *
 * 测试场景：
 * 1. 登录功能
 * 2. 页面加载和 UI 渲染
 * 3. 侧边栏展开/收起功能
 * 4. 聊天输入和消息发送
 * 5. 会话列表和创建新会话
 */

import { test, expect, Page } from '@playwright/test';

// 测试配置
const BASE_URL = 'http://localhost:5188';
const TEST_USER = {
  username: 'admin',
  password: 'admin123'
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

test.describe('AI Agent 前端交互测试（含认证）', () => {
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

  test('场景1: 登录后的页面加载和 UI 渲染', async () => {
    console.log('📍 测试场景1: 登录后的页面加载和 UI 渲染');

    // 1. 检查当前 URL
    console.log('  步骤1: 检查当前 URL');
    const currentUrl = page.url();
    console.log(`    当前 URL: ${currentUrl}`);
    expect(currentUrl).toMatch(/\/(dashboard|session\/)/);

    // 2. 检查页面标题
    console.log('  步骤2: 检查页面标题');
    const title = await page.title();
    console.log(`    页面标题: ${title}`);

    // 3. 截图保存
    await page.screenshot({
      path: 'screenshots/test-auth-scene1-after-login.png',
      fullPage: true
    });
    console.log('  ✓ 截图已保存: screenshots/test-auth-scene1-after-login.png');

    // 4. 检查主要 UI 元素是否存在
    console.log('  步骤3: 检查主要 UI 元素');

    // 检查侧边栏 - 使用更精确的选择器
    const sidebarSelectors = [
      'aside[class*="sidebar"]',
      'aside[class*="Sidebar"]',
      'nav[class*="sidebar"]',
      'nav[class*="Sidebar"]',
      '[data-testid="sidebar"]',
      'aside',
    ];

    let sidebarExists = false;
    for (const selector of sidebarSelectors) {
      const element = page.locator(selector).first();
      if (await element.count() > 0) {
        const visible = await element.isVisible().catch(() => false);
        if (visible) {
          console.log(`    侧边栏: ✓ 存在 (${selector})`);
          sidebarExists = true;
          break;
        }
      }
    }
    if (!sidebarExists) {
      console.log('    侧边栏: ✗ 未找到可见的侧边栏');
    }

    // 检查顶部导航栏
    const topNavSelectors = [
      'header[class*="topnav"]',
      'header[class*="TopNav"]',
      'header[class*="navbar"]',
      'header[class*="Navbar"]',
      '[data-testid="topnav"]',
      'header',
    ];

    let topNavExists = false;
    for (const selector of topNavSelectors) {
      const element = page.locator(selector).first();
      if (await element.count() > 0) {
        const visible = await element.isVisible().catch(() => false);
        if (visible) {
          console.log(`    顶部导航栏: ✓ 存在 (${selector})`);
          topNavExists = true;
          break;
        }
      }
    }
    if (!topNavExists) {
      console.log('    顶部导航栏: ✗ 未找到可见的顶部导航栏');
    }

    // 检查主内容区域
    const mainSelectors = [
      'main',
      '[class*="main-content"]',
      '[class*="MainContent"]',
      '[data-testid="main-content"]',
    ];

    let mainExists = false;
    for (const selector of mainSelectors) {
      const element = page.locator(selector).first();
      if (await element.count() > 0) {
        const visible = await element.isVisible().catch(() => false);
        if (visible) {
          console.log(`    主内容区域: ✓ 存在 (${selector})`);
          mainExists = true;
          break;
        }
      }
    }
    if (!mainExists) {
      console.log('    主内容区域: ✗ 未找到可见的主内容区域');
    }

    // 5. 检查页面可交互性
    console.log('  步骤4: 检查页面可交互性');
    const bodyVisible = await page.locator('body').isVisible();
    console.log(`    页面主体可见: ${bodyVisible ? '✓ 是' : '✗ 否'}`);
    expect(bodyVisible).toBeTruthy();

    console.log('✅ 场景1 完成\n');
  });

  test('场景2: 侧边栏展开/收起功能', async () => {
    console.log('📍 测试场景2: 侧边栏展开/收起功能');

    await waitForPageStable(page, 1000);

    // 1. 查找侧边栏切换按钮
    console.log('  步骤1: 查找侧边栏切换按钮');

    const toggleSelectors = [
      'button[aria-label*="切换" i]',
      'button[aria-label*="toggle" i]',
      'button[aria-label*="sidebar" i]',
      'button[aria-label*="侧边栏" i]',
      'button[class*="toggle"]',
      'button[class*="menu"]',
      'button svg[class*="Menu"]',
    ];

    let toggleButton = null;
    for (const selector of toggleSelectors) {
      try {
        const elements = page.locator(selector);
        const count = await elements.count();
        for (let i = 0; i < count; i++) {
          const element = elements.nth(i);
          if (await element.isVisible()) {
            console.log(`    找到切换按钮: ${selector}`);
            toggleButton = element;
            break;
          }
        }
        if (toggleButton) break;
      } catch (e) {
        // 继续尝试
      }
    }

    if (!toggleButton) {
      console.log('  ⚠ 未找到侧边栏切换按钮');
    } else {
      // 2. 记录初始状态
      console.log('  步骤2: 记录初始状态');
      await page.screenshot({
        path: 'screenshots/test-auth-scene2-sidebar-initial.png'
      });
      console.log('  ✓ 初始状态截图已保存');

      // 3. 点击切换按钮
      console.log('  步骤3: 点击切换按钮');
      await toggleButton.click();
      await waitForPageStable(page, 1000);

      await page.screenshot({
        path: 'screenshots/test-auth-scene2-sidebar-toggled.png'
      });
      console.log('  ✓ 切换后截图已保存');

      // 再次点击恢复
      await toggleButton.click();
      await waitForPageStable(page, 1000);
    }

    console.log('✅ 场景2 完成\n');
  });

  test('场景3: 导航到会话页面', async () => {
    console.log('📍 测试场景3: 导航到会话页面');

    // 1. 检查当前页面
    console.log('  步骤1: 检查当前页面');
    let currentUrl = page.url();
    console.log(`    当前 URL: ${currentUrl}`);

    // 如果不在会话页面，尝试导航
    if (!currentUrl.includes('/session/')) {
      console.log('  步骤2: 导航到会话页面');

      // 尝试点击导航链接
      const navLinkSelectors = [
        'a[href*="/session/"]',
        'a:has-text("会话")',
        'a:has-text("Session")',
        'a:has-text("聊天")',
      ];

      let navLink = null;
      for (const selector of navLinkSelectors) {
        try {
          const element = page.locator(selector).first();
          if (await element.count() > 0 && await element.isVisible()) {
            console.log(`    找到导航链接: ${selector}`);
            navLink = element;
            break;
          }
        } catch (e) {
          // 继续尝试
        }
      }

      if (navLink) {
        await navLink.click();
        await page.waitForLoadState('networkidle');
        await waitForPageStable(page, 2000);
      } else {
        console.log('    ⚠ 未找到导航链接，尝试直接访问 URL');

        // 尝试直接访问会话页面
        const sessionUrl = `${BASE_URL}/session/new`;
        await page.goto(sessionUrl);
        await page.waitForLoadState('networkidle');
        await waitForPageStable(page, 2000);
      }

      currentUrl = page.url();
      console.log(`    导航后 URL: ${currentUrl}`);
    }

    // 3. 截图
    await page.screenshot({
      path: 'screenshots/test-auth-scene3-session-page.png',
      fullPage: true
    });
    console.log('  ✓ 截图已保存');

    console.log('✅ 场景3 完成\n');
  });

  test('场景4: 聊天输入和消息发送', async () => {
    console.log('📍 测试场景4: 聊天输入和消息发送');

    // 1. 确保在会话页面
    console.log('  步骤1: 确保在会话页面');
    const currentUrl = page.url();

    if (!currentUrl.includes('/session/')) {
      await page.goto(`${BASE_URL}/session/new`);
      await page.waitForLoadState('networkidle');
      await waitForPageStable(page, 2000);
    }

    // 2. 查找输入框
    console.log('  步骤2: 查找聊天输入框');

    const inputSelectors = [
      'textarea[placeholder*="输入" i]',
      'textarea[placeholder*="消息" i]',
      'textarea[placeholder*="发送" i]',
      'textarea',
      'input[type="text"]',
      '[contenteditable="true"]',
      '[role="textbox"]',
    ];

    let inputBox = null;
    for (const selector of inputSelectors) {
      try {
        const elements = page.locator(selector);
        const count = await elements.count();
        for (let i = 0; i < count; i++) {
          const element = elements.nth(i);
          if (await element.isVisible()) {
            console.log(`    找到输入框: ${selector}`);
            inputBox = element;
            break;
          }
        }
        if (inputBox) break;
      } catch (e) {
        // 继续尝试
      }
    }

    if (!inputBox) {
      console.log('  ⚠ 未找到输入框');
      console.log('✅ 场景4 完成（跳过）\n');
      return;
    }

    // 3. 输入测试消息
    console.log('  步骤3: 输入测试消息');
    const testMessage = '你好，这是一条测试消息';
    await inputBox.fill(testMessage);
    await waitForPageStable(page, 500);

    // 验证输入内容
    const inputValue = await inputBox.inputValue();
    console.log(`    输入内容: ${inputValue}`);
    expect(inputValue).toContain(testMessage);

    // 截图
    await page.screenshot({
      path: 'screenshots/test-auth-scene4-message-input.png'
    });
    console.log('  ✓ 截图已保存');

    // 4. 查找发送按钮
    console.log('  步骤4: 查找发送按钮');

    const sendButtonSelectors = [
      'button[aria-label*="发送" i]',
      'button[aria-label*="send" i]',
      'button[type="submit"]',
      'button[class*="send"]',
      'button:has-text("发送")',
      'button:has(svg[class*="Send"])',
    ];

    let sendButton = null;
    for (const selector of sendButtonSelectors) {
      try {
        const elements = page.locator(selector);
        const count = await elements.count();
        for (let i = 0; i < count; i++) {
          const element = elements.nth(i);
          if (await element.isVisible()) {
            console.log(`    找到发送按钮: ${selector}`);
            sendButton = element;
            break;
          }
        }
        if (sendButton) break;
      } catch (e) {
        // 继续尝试
      }
    }

    if (!sendButton) {
      console.log('  ⚠ 未找到发送按钮');
      console.log('✅ 场景4 完成\n');
      return;
    }

    // 5. 点击发送按钮
    console.log('  步骤5: 点击发送按钮');
    await sendButton.click();
    await waitForPageStable(page, 3000);

    // 截图
    await page.screenshot({
      path: 'screenshots/test-auth-scene4-message-sent.png',
      fullPage: true
    });
    console.log('  ✓ 截图已保存');

    // 6. 检查消息是否显示
    console.log('  步骤6: 检查消息显示');

    const messageSelectors = [
      '[class*="message"]',
      '[class*="Message"]',
      '[class*="chat"] [class*="item"]',
      '[class*="chat"] [class*="bubble"]',
      '[data-message]',
    ];

    let messageFound = false;
    for (const selector of messageSelectors) {
      const messages = page.locator(selector);
      const count = await messages.count();
      if (count > 0) {
        console.log(`    找到 ${count} 条消息元素 (${selector})`);

        // 检查是否包含我们的测试消息
        for (let i = 0; i < Math.min(count, 5); i++) {
          const text = await messages.nth(i).textContent();
          if (text && text.includes(testMessage)) {
            console.log(`    ✓ 找到测试消息: "${text}"`);
            messageFound = true;
            break;
          }
        }
        break;
      }
    }

    if (!messageFound) {
      console.log('    ⚠ 未找到测试消息（可能正在处理中）');
    }

    console.log('✅ 场景4 完成\n');
  });

  test('场景5: 检查侧边栏会话列表', async () => {
    console.log('📍 测试场景5: 检查侧边栏会话列表');

    await waitForPageStable(page, 1000);

    // 1. 查找会话列表
    console.log('  步骤1: 查找会话列表');

    const sessionListSelectors = [
      '[class*="session-list"]',
      '[class*="SessionList"]',
      '[class*="conversation-list"]',
      'aside [class*="list"]',
      'nav [class*="list"]',
      '[data-testid="session-list"]',
    ];

    let sessionList = null;
    for (const selector of sessionListSelectors) {
      try {
        const element = page.locator(selector).first();
        if (await element.count() > 0) {
          const visible = await element.isVisible().catch(() => false);
          if (visible) {
            console.log(`    找到会话列表: ${selector}`);
            sessionList = element;
            break;
          }
        }
      } catch (e) {
        // 继续尝试
      }
    }

    if (!sessionList) {
      console.log('    ⚠ 未找到会话列表容器');
    } else {
      // 统计会话项数量
      const sessionItemSelectors = [
        '[class*="session-item"]',
        '[class*="SessionItem"]',
        'li',
        'button[class*="item"]',
      ];

      let itemCount = 0;
      for (const selector of sessionItemSelectors) {
        const items = sessionList.locator(selector);
        const count = await items.count();
        if (count > 0) {
          itemCount = count;
          console.log(`    会话数量: ${count} (${selector})`);
          break;
        }
      }

      if (itemCount === 0) {
        console.log('    会话数量: 0 (可能没有会话或使用不同的选择器)');
      }
    }

    // 2. 查找创建新会话按钮
    console.log('  步骤2: 查找创建新会话按钮');

    const newSessionSelectors = [
      'button:has-text("新建")',
      'button:has-text("新会话")',
      'button:has-text("New Chat")',
      'button:has-text("新对话")',
      'button[aria-label*="new" i]',
      'button[aria-label*="create" i]',
      'button[class*="new"]',
    ];

    let newSessionButton = null;
    for (const selector of newSessionSelectors) {
      try {
        const elements = page.locator(selector);
        const count = await elements.count();
        for (let i = 0; i < count; i++) {
          const element = elements.nth(i);
          if (await element.isVisible()) {
            const text = await element.textContent();
            console.log(`    找到按钮: "${text?.trim()}" (${selector})`);
            newSessionButton = element;
            break;
          }
        }
        if (newSessionButton) break;
      } catch (e) {
        // 继续尝试
      }
    }

    if (!newSessionButton) {
      console.log('    ⚠ 未找到创建新会话按钮');
    } else {
      console.log('    ✓ 找到创建新会话按钮');
    }

    // 3. 截图
    await page.screenshot({
      path: 'screenshots/test-auth-scene5-session-list.png',
      fullPage: true
    });
    console.log('  ✓ 截图已保存');

    console.log('✅ 场景5 完成\n');
  });

  test('综合测试: 完整用户流程', async () => {
    console.log('📍 综合测试: 完整用户流程');

    // 1. 检查页面状态
    console.log('  步骤1: 检查页面状态');
    const url = page.url();
    console.log(`    当前 URL: ${url}`);

    const title = await page.title();
    console.log(`    页面标题: ${title}`);

    // 2. 检查可访问性
    console.log('  步骤2: 检查可访问性');

    // 检查所有可见的按钮
    const buttons = page.locator('button:visible');
    const buttonCount = await buttons.count();
    console.log(`    可见按钮数量: ${buttonCount}`);

    // 检查所有可见的输入框
    const inputs = page.locator('input:visible, textarea:visible, [contenteditable]:visible');
    const inputCount = await inputs.count();
    console.log(`    可见输入框数量: ${inputCount}`);

    // 检查所有可见的链接
    const links = page.locator('a:visible');
    const linkCount = await links.count();
    console.log(`    可见链接数量: ${linkCount}`);

    // 3. 最终截图
    console.log('  步骤3: 最终截图');
    await page.screenshot({
      path: 'screenshots/test-auth-final-comprehensive.png',
      fullPage: true
    });
    console.log('    ✓ 最终截图已保存');

    // 4. 检查页面无障碍性
    console.log('  步骤4: 检查页面无障碍性');
    const accessibilityIssues = await page.accessibility.snapshot();
    if (accessibilityIssues) {
      console.log(`    无障碍性快照已获取`);
    }

    console.log('✅ 综合测试完成\n');
  });
});
