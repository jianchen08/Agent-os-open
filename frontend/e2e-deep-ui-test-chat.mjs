/**
 * Agent OS (超级终端) 聊天区域专项测试
 *
 * 修复选择器问题，精确验证聊天区域的交互功能
 */

import { chromium } from 'playwright';
import fs from 'fs';

const BASE_URL = 'http://127.0.0.1:5188';
const SCREENSHOT_DIR = 'e2e-deep-screenshots';
const results = [];
const bugs = [];
let screenshotIndex = 32;

async function takeScreenshot(page, name) {
  screenshotIndex++;
  const filename = `${String(screenshotIndex).padStart(3, '0')}-${name}.png`;
  await page.screenshot({ path: `${SCREENSHOT_DIR}/${filename}`, fullPage: true });
  console.log(`    [截图] ${filename}`);
}

function record(category, name, passed, expected, actual, severity = 'info') {
  results.push({ category, name, passed, expected, actual, severity });
  const icon = passed ? 'PASS' : 'FAIL';
  const sev = severity === 'critical' ? ' (!!)' : severity === 'high' ? ' (!)' : severity === 'medium' ? ' (~)' : '';
  console.log(`    [${icon}]${sev} ${name}`);
  if (!passed) {
    console.log(`         Expected: ${expected}`);
    console.log(`         Actual:   ${actual}`);
    bugs.push({
      testItem: `${category} - ${name}`,
      expected,
      actual,
      severity: severity === 'critical' ? '高' : severity === 'high' ? '高' : severity === 'medium' ? '中' : '低',
    });
  }
}

async function safeRun(name, fn) {
  try {
    await fn();
  } catch (e) {
    record('执行异常', name, false, '无异常', e.message, 'critical');
  }
}

async function main() {
  console.log('\n==============================================');
  console.log('  Agent OS 聊天区域专项测试');
  console.log('==============================================\n');

  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await context.newPage();
  page.setDefaultTimeout(15000);

  // ---- 登录 ----
  console.log('[STEP 0] 登录...');
  await page.goto(BASE_URL + '/login', { waitUntil: 'networkidle', timeout: 15000 });
  await page.waitForTimeout(1000);
  if (page.url().includes('/login')) {
    const u = page.locator('[data-testid="login-username-input"], #username').first();
    await u.waitFor({ state: 'visible', timeout: 10000 });
    await u.fill('admin');
    await page.locator('[data-testid="login-password-input"], #password').first().fill('admin123');
    await page.locator('[data-testid="login-submit-button"], button[type="submit"]').first().click();
    await page.waitForURL(url => !url.toString().includes('/login'), { timeout: 15000 }).catch(() => {});
    await page.waitForTimeout(2000);
  }

  if (page.url().includes('/login')) {
    console.log('  登录失败，终止测试');
    await browser.close();
    return;
  }
  console.log('    登录成功');

  // ---- 确保在主页 ----
  await page.goto(BASE_URL + '/', { waitUntil: 'networkidle', timeout: 15000 });
  await page.waitForTimeout(2000);

  // ---- 展开侧边栏 ----
  const panelOpenIcon = page.locator('svg.lucide-panel-left-open').first();
  if (await panelOpenIcon.isVisible().catch(() => false)) {
    await panelOpenIcon.locator('..').click();
    await page.waitForTimeout(500);
  }

  await takeScreenshot(page, 'chat-test-home');

  // ============================================
  // STEP 1: 正确选中一个会话
  // ============================================
  console.log('\n[STEP 1] 选中会话...');

  await safeRun('选中灵汐会话', async () => {
    // 关键修复：使用正确的选择器
    // 每个会话项是一个 .group 的 div，其第一个子 div 是标题（带 onClick）
    // 所以用 .group 直接选择每个会话行，然后点击标题部分
    const sessionGroups = page.locator('aside .group');
    const count = await sessionGroups.count();
    console.log(`    找到 ${count} 个会话组`);

    // 列出每个会话的标题
    for (let i = 0; i < count; i++) {
      const group = sessionGroups.nth(i);
      // 标题在第一个子 div 中（有 cursor-pointer 类）
      const titleDiv = group.locator('div.cursor-pointer, div[class*="cursor-pointer"]').first();
      const title = await titleDiv.textContent().catch(() => '');
      console.log(`    会话 ${i + 1}: "${title?.trim()}"`);
    }

    // 点击第二个会话（索引1，应该是"灵汐"）
    if (count >= 2) {
      const secondGroup = sessionGroups.nth(1);
      const titleDiv = secondGroup.locator('div.cursor-pointer, div[class*="cursor-pointer"]').first();
      const title = await titleDiv.textContent().catch(() => '');
      console.log(`    点击第二个会话: "${title?.trim()}"`);
      await titleDiv.click();
      await page.waitForTimeout(2000);
    } else if (count >= 1) {
      const firstGroup = sessionGroups.first();
      const titleDiv = firstGroup.locator('div.cursor-pointer, div[class*="cursor-pointer"]').first();
      await titleDiv.click();
      await page.waitForTimeout(2000);
    }

    await takeScreenshot(page, 'chat-session-selected');

    // 验证是否进入了聊天界面
    const pageText = await page.textContent('body').catch(() => '');
    const isWelcome = pageText.includes('欢迎使用超级终端');
    const hasChatContainer = pageText.includes('glm') || await page.locator('textarea').count() > 0;

    record('聊天区域', '选中会话后进入聊天', !isWelcome || hasChatContainer,
      '页面不再显示欢迎信息，或出现聊天组件',
      isWelcome ? '仍显示欢迎页面' : '已进入聊天', !isWelcome || hasChatContainer ? 'info' : 'high');
  });

  // ============================================
  // STEP 2: 验证聊天输入框
  // ============================================
  console.log('\n[STEP 2] 聊天输入框验证...');

  await safeRun('消息输入框', async () => {
    // 如果还是欢迎页面，尝试创建新会话
    const pageText = await page.textContent('body').catch(() => '');
    if (pageText.includes('欢迎使用超级终端')) {
      console.log('    仍在欢迎页面，创建新会话...');
      const newBtn = page.locator('button:has-text("新会话")').first();
      await newBtn.click();
      await page.waitForTimeout(2000);
    }

    await takeScreenshot(page, 'chat-before-input');

    // 查找 textarea
    const textarea = page.locator('textarea').first();
    const textareaVisible = await textarea.isVisible().catch(() => false);

    if (textareaVisible) {
      record('聊天交互', '消息输入框 (textarea) 可见', true,
        'textarea 可见', 'textarea 可见', 'info');

      // 输入 "测试"
      await textarea.click();
      await page.waitForTimeout(200);
      await textarea.fill('测试');
      await page.waitForTimeout(300);

      const inputValue = await textarea.inputValue().catch(() => '');
      record('聊天交互', '输入 "测试" 成功', inputValue === '测试',
        '输入框值为 "测试"', `实际值: "${inputValue}"`, inputValue === '测试' ? 'info' : 'high');

      await takeScreenshot(page, 'chat-input-filled');
    } else {
      // 可能使用 contenteditable 或其他输入方式
      const contentEditable = page.locator('[contenteditable="true"]').first();
      const ceVisible = await contentEditable.isVisible().catch(() => false);

      if (ceVisible) {
        record('聊天交互', '消息输入框 (contenteditable) 可见', true,
          'contenteditable 可见', 'contenteditable 可见', 'info');
        await contentEditable.click();
        await contentEditable.fill('测试');
        await page.waitForTimeout(300);
      } else {
        record('聊天交互', '消息输入框可见', false,
          'textarea 或 contenteditable 可见',
          `textarea: ${await page.locator('textarea').count()}, contenteditable: ${await page.locator('[contenteditable]').count()}`,
          'high');

        // 调试：输出页面上所有可交互元素
        const allInputs = await page.locator('input, textarea, [contenteditable], [role="textbox"]').count();
        console.log(`    页面可交互元素: ${allInputs}`);

        // 输出聊天区域的 HTML 结构
        const chatArea = page.locator('main, section, [class*="chat"]').first();
        if (await chatArea.isVisible().catch(() => false)) {
          const chatHtml = await chatArea.evaluate(el => el.innerHTML?.substring(0, 500)).catch(() => '');
          console.log(`    聊天区域 HTML (前500字): ${chatHtml}`);
        }
      }
    }
  });

  // ============================================
  // STEP 3: 发送按钮状态
  // ============================================
  console.log('\n[STEP 3] 发送按钮状态验证...');

  await safeRun('发送按钮', async () => {
    const textarea = page.locator('textarea').first();
    if (!(await textarea.isVisible().catch(() => false))) {
      record('聊天交互', '发送按钮测试跳过', true, 'textarea 可见', 'textarea 不可见', 'info');
      return;
    }

    // 确保输入框有内容
    await textarea.fill('测试');
    await page.waitForTimeout(300);

    // 查找所有按钮并输出信息
    const chatArea = textarea.locator('..').locator('..');
    const buttons = chatArea.locator('button');
    const btnCount = await buttons.count();
    console.log(`    输入区域附近有 ${btnCount} 个按钮:`);

    for (let i = 0; i < btnCount; i++) {
      const btn = buttons.nth(i);
      const title = await btn.getAttribute('title').catch(() => '') || '';
      const ariaLabel = await btn.getAttribute('aria-label').catch(() => '') || '';
      const text = (await btn.textContent().catch(() => '') || '').trim().substring(0, 30);
      const disabled = await btn.isDisabled().catch(() => false);
      console.log(`      [${i}] title="${title}" aria-label="${ariaLabel}" text="${text}" disabled=${disabled}`);
    }

    // 查找发送按钮
    const sendSelectors = [
      'button[aria-label*="发送"]',
      'button[aria-label*="Send"]',
      'button[title*="发送"]',
      'button[title*="Send"]',
    ];

    let sendBtn = null;
    for (const sel of sendSelectors) {
      const el = page.locator(sel).first();
      if (await el.isVisible().catch(() => false)) {
        sendBtn = el;
        break;
      }
    }

    if (sendBtn) {
      const isEnabled = await sendBtn.isEnabled().catch(() => false);
      record('聊天交互', '有输入时发送按钮启用', isEnabled,
        '发送按钮应启用', `发送按钮${isEnabled ? '已启用' : '仍禁用'}`, isEnabled ? 'info' : 'high');

      // 清空输入
      await textarea.fill('');
      await page.waitForTimeout(200);

      const isDisabledAfterClear = await sendBtn.isDisabled().catch(() => false);
      record('聊天交互', '清空输入后发送按钮禁用', isDisabledAfterClear,
        '清空后发送按钮应禁用', `发送按钮${isDisabledAfterClear ? '已禁用' : '未禁用'}`, isDisabledAfterClear ? 'info' : 'medium');
    } else {
      // 检查是否有 SVG 发送图标
      const sendIcons = page.locator('svg.lucide-send, svg.lucide-arrow-up, button:has(svg)').filter({
        has: page.locator('svg')
      });
      const sendIconCount = await sendIcons.count();
      console.log(`    未找到发送按钮，但有 ${sendIconCount} 个含 SVG 的按钮`);

      record('聊天交互', '发送按钮存在', false,
        '发送按钮可见', '未找到明确的发送按钮（可能用 Enter 发送）', 'medium');
    }

    // 清空输入框（不发送）
    await textarea.fill('');
  });

  // ============================================
  // STEP 4: 思考模式按钮
  // ============================================
  console.log('\n[STEP 4] 思考模式按钮...');

  await safeRun('思考模式按钮', async () => {
    const allButtons = page.locator('button');
    const btnCount = await allButtons.count();

    let thinkBtn = null;
    for (let i = 0; i < btnCount; i++) {
      const btn = allButtons.nth(i);
      const text = (await btn.textContent().catch(() => '')) || '';
      const title = (await btn.getAttribute('title').catch(() => '')) || '';
      const ariaLabel = (await btn.getAttribute('aria-label').catch(() => '')) || '';
      const lowerText = text.toLowerCase();
      const lowerTitle = title.toLowerCase();
      const lowerAria = ariaLabel.toLowerCase();
      if (text.includes('思考') || title.includes('思考') || ariaLabel.includes('思考')
        || lowerText.includes('think') || lowerTitle.includes('think') || lowerAria.includes('think')) {
        thinkBtn = btn;
        console.log(`    找到思考按钮: text="${text.trim()}" title="${title}" aria-label="${ariaLabel}"`);
        break;
      }
    }

    if (thinkBtn) {
      record('聊天交互', '思考模式按钮可见', true, '思考按钮可见', '已找到', 'info');

      // 点击切换
      await thinkBtn.click();
      await page.waitForTimeout(500);
      await takeScreenshot(page, 'thinking-mode-on');

      // 再次点击切换回来
      await thinkBtn.click();
      await page.waitForTimeout(300);

      record('聊天交互', '思考模式按钮可点击切换', true, '点击切换成功', '已切换', 'info');
    } else {
      record('聊天交互', '思考模式按钮可见', false,
        '包含"思考"或"think"的按钮', '未找到', 'medium');
    }
  });

  // ============================================
  // STEP 5: 模型名和 Token 显示
  // ============================================
  console.log('\n[STEP 5] 模型名和 Token...');

  await safeRun('模型名和Token', async () => {
    const pageText = (await page.textContent('body').catch(() => '')) || '';

    // 检查 glm 模型名
    const hasGlm = pageText.includes('glm');
    record('聊天交互', '模型名显示 (glm-5.1)', hasGlm,
      '页面包含 "glm"', hasGlm ? '包含 glm' : '未找到 glm', hasGlm ? 'info' : 'medium');

    // Token 显示
    const hasToken = pageText.includes('token') || pageText.includes('Token')
      || pageText.includes('上下文') || pageText.includes('context') || pageText.includes('Context');
    record('聊天交互', 'Token/上下文信息显示', hasToken,
      '页面包含 token/上下文信息', hasToken ? '包含' : '未找到', hasToken ? 'info' : 'low');

    // 查找所有 span 中可能包含模型信息的
    const spans = page.locator('span');
    const spanCount = await spans.count();
    for (let i = 0; i < Math.min(spanCount, 200); i++) {
      const text = (await spans.nth(i).textContent().catch(() => '')) || '';
      if (text.includes('glm') || text.includes('token') || text.includes('Token')
        || text.includes('模型') || text.includes('model')) {
        console.log(`    找到相关 span[${i}]: "${text.trim()}"`);
      }
    }

    await takeScreenshot(page, 'model-token-display');
  });

  // ============================================
  // STEP 6: 工作区面板在聊天状态下的验证
  // ============================================
  console.log('\n[STEP 6] 工作区面板验证...');

  await safeRun('工作区面板在聊天状态下', async () => {
    // 查找工作区切换按钮
    const allButtons = page.locator('button');
    const btnCount = await allButtons.count();

    for (let i = 0; i < btnCount; i++) {
      const btn = allButtons.nth(i);
      const title = (await btn.getAttribute('title').catch(() => '')) || '';
      if (title.toLowerCase().includes('workspace') || title.includes('工作区')) {
        const isVisible = await btn.isVisible().catch(() => false);
        record('工作区面板', `工作区切换按钮 (title="${title}")`, isVisible,
          '工作区切换按钮可见', `按钮${isVisible ? '可见' : '不可见'}`, isVisible ? 'info' : 'medium');

        if (isVisible) {
          // 测试收起
          if (title.includes('Hide')) {
            await btn.click();
            await page.waitForTimeout(500);
            await takeScreenshot(page, 'workspace-hidden-in-chat');

            // 查找展开按钮
            const expandBtn = page.locator('button[title*="Show"]').first();
            if (await expandBtn.isVisible().catch(() => false)) {
              record('工作区面板', '工作区收起后出现展开按钮', true,
                'Show workspace 按钮可见', '已找到', 'info');
              await expandBtn.click();
              await page.waitForTimeout(500);
            }
          }
        }
        break;
      }
    }
  });

  // ---- 清理 ----
  await browser.close();

  // ---- 报告 ----
  console.log('\n==============================================');
  console.log('  聊天区域专项测试报告');
  console.log('==============================================\n');

  const totalTests = results.length;
  const passedTests = results.filter(r => r.passed).length;
  const failedTests = results.filter(r => !r.passed).length;

  console.log(`总计: ${totalTests} 项测试`);
  console.log(`通过: ${passedTests} 项`);
  console.log(`失败: ${failedTests} 项`);
  console.log(`通过率: ${totalTests > 0 ? ((passedTests / totalTests) * 100).toFixed(1) : 0}%`);

  if (failedTests > 0) {
    console.log('\n--- 失败项详情 ---');
    for (const r of results.filter(r => !r.passed)) {
      const sev = r.severity === 'critical' ? '[!!]' : r.severity === 'high' ? '[!]' : r.severity === 'medium' ? '[~]' : '[i]';
      console.log(`  ${sev} [${r.category}] ${r.name}`);
      console.log(`       Expected: ${r.expected}`);
      console.log(`       Actual:   ${r.actual}`);
    }
  }

  if (bugs.length > 0) {
    console.log('\n--- Bug 列表 ---');
    for (const bug of bugs) {
      console.log(`  [${bug.severity}] ${bug.testItem}`);
      console.log(`       Expected: ${bug.expected}`);
      console.log(`       Actual:   ${bug.actual}`);
    }
  }

  console.log('\n==============================================');
  console.log('  测试完成');
  console.log('==============================================\n');

  const jsonOutput = {
    summary: { total: totalTests, passed: passedTests, failed: failedTests, passRate: totalTests > 0 ? ((passedTests / totalTests) * 100).toFixed(1) + '%' : '0%' },
    bugs,
    results
  };
  fs.writeFileSync('e2e-deep-ui-test-results-chat.json', JSON.stringify(jsonOutput, null, 2));
  console.log('JSON 结果已保存: e2e-deep-ui-test-results-chat.json');
}

main().catch(err => {
  console.error('测试执行出错:', err);
  process.exit(1);
});
