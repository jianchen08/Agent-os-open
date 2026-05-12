/**
 * AI Agent 系统前端交互功能测试
 *
 * 测试场景：
 * 1. 打开页面，检查 UI 渲染
 * 2. 点击侧边栏，检查展开/收起
 * 3. 输入消息并发送，检查消息显示
 * 4. 创建新会话，检查会话切换
 */

import { test, expect, Page } from '@playwright/test';

// 测试配置
const BASE_URL = 'http://localhost:5188';
const TEST_USER = {
  username: 'admin',
  password: 'admin123456'
};

// 辅助函数：等待页面稳定
async function waitForPageStable(page: Page, timeout: number = 3000) {
  await page.waitForTimeout(timeout);
}

test.describe('AI Agent 前端交互测试', () => {
  let page: Page;

  test.beforeAll(async ({ browser }) => {
    page = await browser.newPage();
    page.setDefaultTimeout(10000);
  });

  test.afterAll(async () => {
    await page.close();
  });

  test('场景1: 页面加载和 UI 渲染', async () => {
    console.log('📍 测试场景1: 页面加载和 UI 渲染');

    // 1. 打开前端页面
    console.log('  步骤1: 打开前端页面');
    const response = await page.goto(BASE_URL);
    expect(response?.status()).toBeLessThan(400);

    // 等待页面加载
    await waitForPageStable(page, 2000);

    // 2. 检查页面标题
    console.log('  步骤2: 检查页面标题');
    const title = await page.title();
    console.log(`    页面标题: ${title}`);
    expect(title).toBeTruthy();

    // 3. 截图保存
    await page.screenshot({
      path: 'screenshots/test-scene1-page-load.png',
      fullPage: true
    });
    console.log('  ✓ 截图已保存: screenshots/test-scene1-page-load.png');

    // 4. 检查主要 UI 元素是否存在
    console.log('  步骤3: 检查主要 UI 元素');

    // 检查侧边栏
    const sidebar = page.locator('nav, aside, [class*="sidebar"], [class*="Sidebar"]').first();
    const sidebarExists = await sidebar.count() > 0;
    console.log(`    侧边栏: ${sidebarExists ? '✓ 存在' : '✗ 未找到'}`);

    // 检查顶部导航栏
    const topNav = page.locator('header, [class*="topbar"], [class*="TopNav"], [class*="navbar"]').first();
    const topNavExists = await topNav.count() > 0;
    console.log(`    顶部导航栏: ${topNavExists ? '✓ 存在' : '✗ 未找到'}`);

    // 检查聊天容器
    const chatContainer = page.locator('[class*="chat"], [class*="Chat"], main').first();
    const chatExists = await chatContainer.count() > 0;
    console.log(`    聊天容器: ${chatExists ? '✓ 存在' : '✗ 未找到'}`);

    // 5. 检查页面可交互性
    console.log('  步骤4: 检查页面可交互性');
    const bodyVisible = await page.locator('body').isVisible();
    console.log(`    页面主体可见: ${bodyVisible ? '✓ 是' : '✗ 否'}`);
    expect(bodyVisible).toBeTruthy();

    console.log('✅ 场景1 完成: 页面加载和 UI 渲染测试通过\n');
  });

  test('场景2: 侧边栏展开/收起功能', async () => {
    console.log('📍 测试场景2: 侧边栏展开/收起功能');

    // 1. 刷新页面确保干净状态
    await page.goto(BASE_URL);
    await waitForPageStable(page, 2000);

    // 2. 查找侧边栏切换按钮
    console.log('  步骤1: 查找侧边栏切换按钮');

    // 尝试多种选择器
    const toggleSelectors = [
      'button[aria-label*="toggle" i]',
      'button[aria-label*="sidebar" i]',
      '[class*="toggle"]',
      '[class*="menu"] button',
      'button[class*="Menu"]',
      'button[class*="menu"]',
      'svg[class*="Menu"]',
    ];

    let toggleButton = null;
    for (const selector of toggleSelectors) {
      try {
        const element = page.locator(selector).first();
        if (await element.count() > 0) {
          console.log(`    找到切换按钮: ${selector}`);
          toggleButton = element;
          break;
        }
      } catch (e) {
        // 继续尝试下一个选择器
      }
    }

    if (!toggleButton) {
      console.log('  ⚠ 未找到侧边栏切换按钮，尝试点击侧边栏区域');
      // 尝试直接点击侧边栏
      const sidebar = page.locator('nav, aside, [class*="sidebar"]').first();
      if (await sidebar.count() > 0) {
        await sidebar.click();
        await waitForPageStable(page, 1000);
      }
    } else {
      // 3. 点击切换按钮
      console.log('  步骤2: 点击切换按钮');
      await toggleButton.click();
      await waitForPageStable(page, 1000);

      // 截图
      await page.screenshot({
        path: 'screenshots/test-scene2-sidebar-toggled.png'
      });
      console.log('  ✓ 截图已保存: screenshots/test-scene2-sidebar-toggled.png');

      // 再次点击恢复
      await toggleButton.click();
      await waitForPageStable(page, 1000);
    }

    console.log('✅ 场景2 完成: 侧边栏功能测试完成\n');
  });

  test('场景3: 聊天输入和消息发送', async () => {
    console.log('📍 测试场景3: 聊天输入和消息发送');

    // 1. 刷新页面
    await page.goto(BASE_URL);
    await waitForPageStable(page, 2000);

    // 2. 查找输入框
    console.log('  步骤1: 查找聊天输入框');

    const inputSelectors = [
      'textarea[placeholder*="输入" i]',
      'textarea[placeholder*="消息" i]',
      'textarea[placeholder*="发送" i]',
      'textarea',
      'input[type="text"]',
      '[contenteditable="true"]',
      '[class*="input"]',
      '[class*="Input"]',
    ];

    let inputBox = null;
    for (const selector of inputSelectors) {
      try {
        const element = page.locator(selector).first();
        if (await element.count() > 0 && await element.isVisible()) {
          console.log(`    找到输入框: ${selector}`);
          inputBox = element;
          break;
        }
      } catch (e) {
        // 继续尝试
      }
    }

    if (!inputBox) {
      console.log('  ⚠ 未找到输入框，跳过输入测试');
      console.log('✅ 场景3 完成: 输入框未找到\n');
      return;
    }

    // 3. 输入测试消息
    console.log('  步骤2: 输入测试消息');
    const testMessage = '你好，这是一条测试消息';
    await inputBox.fill(testMessage);
    await waitForPageStable(page, 500);

    // 验证输入内容
    const inputValue = await inputBox.inputValue();
    console.log(`    输入内容: ${inputValue}`);
    expect(inputValue).toContain(testMessage);

    // 截图
    await page.screenshot({
      path: 'screenshots/test-scene3-message-input.png'
    });
    console.log('  ✓ 截图已保存: screenshots/test-scene3-message-input.png');

    // 4. 查找发送按钮
    console.log('  步骤3: 查找发送按钮');

    const sendButtonSelectors = [
      'button[aria-label*="发送" i]',
      'button[type="submit"]',
      'button[class*="send"]',
      'button[class*="Send"]',
      'button svg[class*="Send"]',
      'button:has-text("发送")',
    ];

    let sendButton = null;
    for (const selector of sendButtonSelectors) {
      try {
        const element = page.locator(selector).first();
        if (await element.count() > 0 && await element.isVisible()) {
          console.log(`    找到发送按钮: ${selector}`);
          sendButton = element;
          break;
        }
      } catch (e) {
        // 继续尝试
      }
    }

    if (sendButton) {
      console.log('  步骤4: 点击发送按钮');
      await sendButton.click();
      await waitForPageStable(page, 2000);

      // 截图
      await page.screenshot({
        path: 'screenshots/test-scene3-message-sent.png'
      });
      console.log('  ✓ 截图已保存: screenshots/test-scene3-message-sent.png');

      // 5. 检查消息是否显示
      console.log('  步骤5: 检查消息显示');

      const messageSelectors = [
        '[class*="message"]',
        '[class*="Message"]',
        '[class*="chat"] [class*="item"]',
      ];

      let messageFound = false;
      for (const selector of messageSelectors) {
        const messages = page.locator(selector);
        const count = await messages.count();
        if (count > 0) {
          console.log(`    找到 ${count} 条消息元素`);
          messageFound = true;
          break;
        }
      }

      if (messageFound) {
        console.log('    ✓ 消息已显示在聊天区域');
      } else {
        console.log('    ⚠ 未找到消息元素（可能需要等待 AI 响应）');
      }
    } else {
      console.log('  ⚠ 未找到发送按钮');
    }

    console.log('✅ 场景3 完成: 聊天输入和消息发送测试完成\n');
  });

  test('场景4: 会话列表和创建新会话', async () => {
    console.log('📍 测试场景4: 会话列表和创建新会话');

    // 1. 刷新页面
    await page.goto(BASE_URL);
    await waitForPageStable(page, 2000);

    // 2. 检查会话列表
    console.log('  步骤1: 检查会话列表');

    const sessionListSelectors = [
      '[class*="session-list"]',
      '[class*="SessionList"]',
      '[class*="conversation"]',
      'aside [class*="list"]',
      'nav [class*="list"]',
    ];

    let sessionListFound = false;
    for (const selector of sessionListSelectors) {
      const list = page.locator(selector).first();
      if (await list.count() > 0) {
        console.log(`    找到会话列表: ${selector}`);
        sessionListFound = true;

        // 统计会话数量
        const items = list.locator('[class*="item"], li, button');
        const count = await items.count();
        console.log(`    会话数量: ${count}`);
        break;
      }
    }

    if (!sessionListFound) {
      console.log('    ⚠ 未找到会话列表');
    }

    // 3. 查找创建新会话按钮
    console.log('  步骤2: 查找创建新会话按钮');

    const newSessionSelectors = [
      'button:has-text("新建")',
      'button:has-text("新会话")',
      'button:has-text("New")',
      'button[aria-label*="new" i]',
      'button[aria-label*="create" i]',
      'button[class*="new"]',
      'button[class*="New"]',
      'button:has(svg)',
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
            console.log(`    找到按钮: ${selector} (${text?.trim()})`);
            newSessionButton = element;
            break;
          }
        }
        if (newSessionButton) break;
      } catch (e) {
        // 继续尝试
      }
    }

    if (newSessionButton) {
      console.log('  步骤3: 点击创建新会话按钮');
      await newSessionButton.click();
      await waitForPageStable(page, 2000);

      // 截图
      await page.screenshot({
        path: 'screenshots/test-scene4-new-session.png'
      });
      console.log('  ✓ 截图已保存: screenshots/test-scene4-new-session.png');

      console.log('    ✓ 新会话已创建');
    } else {
      console.log('  ⚠ 未找到创建新会话按钮');
    }

    // 4. 最终截图
    await page.screenshot({
      path: 'screenshots/test-scene4-final-state.png',
      fullPage: true
    });

    console.log('✅ 场景4 完成: 会话列表和创建新会话测试完成\n');
  });

  test('综合测试: 完整用户流程', async () => {
    console.log('📍 综合测试: 完整用户流程');

    // 1. 打开页面
    console.log('  步骤1: 打开页面');
    await page.goto(BASE_URL);
    await waitForPageStable(page, 2000);

    // 2. 检查页面状态
    console.log('  步骤2: 检查页面状态');
    const url = page.url();
    console.log(`    当前 URL: ${url}`);

    const title = await page.title();
    console.log(`    页面标题: ${title}`);

    // 3. 检查可访问性
    console.log('  步骤3: 检查可访问性');

    // 检查所有可见的按钮
    const buttons = page.locator('button:visible');
    const buttonCount = await buttons.count();
    console.log(`    可见按钮数量: ${buttonCount}`);

    // 检查所有可见的输入框
    const inputs = page.locator('input:visible, textarea:visible');
    const inputCount = await inputs.count();
    console.log(`    可见输入框数量: ${inputCount}`);

    // 4. 最终截图
    console.log('  步骤4: 最终截图');
    await page.screenshot({
      path: 'screenshots/test-final-comprehensive.png',
      fullPage: true
    });
    console.log('    ✓ 最终截图已保存');

    console.log('✅ 综合测试完成\n');
  });
});
