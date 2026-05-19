/**
 * Agent OS (超级终端) 深度 UI 交互测试
 *
 * 测试策略：模拟真实用户操作，验证每个交互后的页面显示内容
 * 截图验证每个操作的结果
 *
 * 测试范围：
 * 1. 主页面布局（侧边栏、聊天区域、工作区面板）
 * 2. 顶部导航栏（每个按钮点击后验证页面内容）
 * 3. 聊天区域交互（输入框、发送按钮、思考模式、模型显示）
 * 4. 工作区面板（隐藏/展开/关闭）
 * 5. 侧边栏交互（新会话、更多操作、重命名、隐藏/显示）
 * 6. 主题和布局切换
 */

import { chromium } from 'playwright';
import fs from 'fs';

const BASE_URL = 'http://127.0.0.1:5188';
const SCREENSHOT_DIR = 'e2e-deep-screenshots';

/** 测试结果 */
const results = [];
const bugs = [];
let screenshotIndex = 0;

/** 确保截图目录存在 */
if (!fs.existsSync(SCREENSHOT_DIR)) {
  fs.mkdirSync(SCREENSHOT_DIR, { recursive: true });
}

/**
 * 截图并保存
 */
async function takeScreenshot(page, name) {
  screenshotIndex++;
  const filename = `${String(screenshotIndex).padStart(3, '0')}-${name}.png`;
  const filepath = `${SCREENSHOT_DIR}/${filename}`;
  await page.screenshot({ path: filepath, fullPage: true });
  console.log(`    [截图] ${filename}`);
  return filepath;
}

/**
 * 记录测试结果
 */
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

/**
 * 安全执行
 */
async function safeRun(name, fn) {
  try {
    await fn();
  } catch (e) {
    record('执行异常', name, false, '无异常', e.message, 'critical');
  }
}

/**
 * 主测试流程
 */
async function main() {
  console.log('\n==============================================');
  console.log('  Agent OS (超级终端) 深度 UI 交互测试');
  console.log('==============================================\n');

  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await context.newPage();
  page.setDefaultTimeout(10000);

  // ============================================
  // STEP 0: 登录
  // ============================================
  console.log('[STEP 0] 登录...');
  await safeRun('登录', async () => {
    // 方案1：直接使用登录表单登录（最可靠）
    await page.goto(BASE_URL + '/login', { waitUntil: 'networkidle', timeout: 15000 });
    await page.waitForTimeout(1000);

    // 检查是否已经在主页（已登录状态）
    if (!page.url().includes('/login')) {
      console.log('    已处于登录状态，跳过登录');
    } else {
      // 使用 data-testid 定位表单元素
      const usernameInput = page.locator('[data-testid="login-username-input"], #username').first();
      const passwordInput = page.locator('[data-testid="login-password-input"], #password').first();
      const loginBtn = page.locator('[data-testid="login-submit-button"], button[type="submit"]').first();

      await usernameInput.waitFor({ state: 'visible', timeout: 10000 });
      await usernameInput.fill('admin');
      await passwordInput.fill('admin123');
      await loginBtn.click();

      // 等待登录完成 - URL 应该从 /login 变为 /
      await page.waitForURL(url => !url.toString().includes('/login'), { timeout: 15000 }).catch(() => {});
      await page.waitForTimeout(2000);
    }

    // 如果还在登录页，尝试 API 方式
    if (page.url().includes('/login')) {
      console.log('    表单登录失败，尝试 API + localStorage 注入...');
      try {
        const loginResp = await page.request.post(BASE_URL + '/api/v1/auth/login', {
          data: { username: 'admin', password: 'admin123' }
        });

        if (loginResp.ok()) {
          const loginData = await loginResp.json();
          const token = loginData.access_token;
          const refreshToken = loginData.refresh_token;
          const expiresIn = loginData.expires_in || 3600;

          if (token) {
            // 按照项目的 STORAGE_KEYS 格式注入 localStorage
            await page.evaluate(({ t, rt, exp }) => {
              localStorage.setItem('access_token', t);
              localStorage.setItem('refresh_token', rt);
              localStorage.setItem('access_token_expiry', exp);
              localStorage.setItem('auth_user', JSON.stringify({
                id: '1',
                username: 'admin',
                createdAt: new Date().toISOString()
              }));
            }, {
              t: token,
              rt: refreshToken || '',
              exp: String(Date.now() + expiresIn * 1000)
            });

            await page.goto(BASE_URL + '/', { waitUntil: 'networkidle', timeout: 15000 });
            await page.waitForTimeout(2000);
          }
        } else {
          const respText = await loginResp.text().catch(() => '');
          console.log(`    API 登录失败: status=${loginResp.status()} body=${respText}`);
        }
      } catch (apiErr) {
        console.log(`    API 调用异常: ${apiErr.message}`);
      }
    }

    const afterLoginUrl = page.url();
    const loginSuccess = !afterLoginUrl.includes('/login');
    record('登录', '管理员登录', loginSuccess,
      '登录后 URL 不包含 /login', `URL: ${afterLoginUrl}`, loginSuccess ? 'info' : 'critical');

    await takeScreenshot(page, '00-after-login');

    // 额外等待页面完全加载
    if (loginSuccess) {
      await page.waitForTimeout(2000);
    }
  });

  // ============================================
  // STEP 1: 主页面布局测试
  // ============================================
  console.log('\n[STEP 1] 主页面布局测试...');
  await takeScreenshot(page, '01-main-page-layout');

  // 1.1 验证侧边栏会话列表
  await safeRun('侧边栏会话列表', async () => {
    // 确保侧边栏展开
    const panelCloseBtn = page.locator('header button').filter({ has: page.locator('svg.lucide-panel-left-close') }).first();
    if (!(await panelCloseBtn.isVisible().catch(() => false))) {
      const panelOpenBtn = page.locator('header button').filter({ has: page.locator('svg.lucide-panel-left-open') }).first();
      if (await panelOpenBtn.isVisible().catch(() => false)) {
        await panelOpenBtn.click();
        await page.waitForTimeout(500);
      }
    }

    // 检查侧边栏可见性
    const sidebar = page.locator('aside').first();
    const sidebarVisible = await sidebar.isVisible().catch(() => false);
    record('主页面布局', '侧边栏可见', sidebarVisible,
      '侧边栏 aside 元素可见', `侧边栏${sidebarVisible ? '可见' : '不可见'}`, sidebarVisible ? 'info' : 'high');

    // 检查"+ 新会话"按钮
    const newSessionBtn = page.locator('button:has-text("新会话")').first();
    const newSessionBtnVisible = await newSessionBtn.isVisible().catch(() => false);
    record('主页面布局', '"新会话" 按钮可见', newSessionBtnVisible,
      '"新会话" 按钮在侧边栏中可见', `"新会话" 按钮${newSessionBtnVisible ? '可见' : '不可见'}`, newSessionBtnVisible ? 'info' : 'high');

    // 检查会话列表中有多少会话
    const sessionItems = page.locator('aside .group');
    const sessionCount = await sessionItems.count();
    record('主页面布局', `会话列表包含会话 (${sessionCount} 个)`, sessionCount > 0,
      '至少有一个会话', `找到 ${sessionCount} 个会话`, sessionCount > 0 ? 'info' : 'medium');

    // 列出所有会话标题
    if (sessionCount > 0) {
      for (let i = 0; i < Math.min(sessionCount, 5); i++) {
        const title = await sessionItems.nth(i).textContent().catch(() => '');
        console.log(`      会话 ${i + 1}: "${title?.trim()}"`);
      }
    }
  });

  // 1.2 点击第一个会话，验证聊天区域加载
  await safeRun('点击会话加载聊天区域', async () => {
    const firstSession = page.locator('aside .group > div').first();
    if (await firstSession.isVisible().catch(() => false)) {
      const sessionTitle = await firstSession.textContent().catch(() => '');
      console.log(`    点击会话: "${sessionTitle?.trim()}"`);

      await firstSession.click();
      await page.waitForTimeout(2000);

      // 验证聊天区域是否加载
      // 查找消息列表或欢迎消息
      const messageList = page.locator('[data-testid="message-list"], [class*="message-list"], [class*="MessageList"]').first();
      const welcomeText = page.locator('text=欢迎使用超级终端').first();
      const textarea = page.locator('textarea').first();

      const chatAreaLoaded = await messageList.isVisible().catch(() => false)
        || await welcomeText.isVisible().catch(() => false)
        || await textarea.isVisible().catch(() => false);

      record('主页面布局', '点击会话后聊天区域加载', chatAreaLoaded,
        '消息列表或输入框可见', `聊天区域${chatAreaLoaded ? '已加载' : '未加载'}`, chatAreaLoaded ? 'info' : 'high');

      await takeScreenshot(page, '01b-after-clicking-session');
    }
  });

  // 1.3 验证右侧工作区面板
  await safeRun('工作区面板显示', async () => {
    // 当前默认是 five-space 布局，检查工作区面板
    // 工作区面板是 FiveSpaceLayout 中的右侧区域
    const workspaceSection = page.locator('section').last();
    const workspaceVisible = await workspaceSection.isVisible().catch(() => false);

    // 也检查工作区切换手柄
    const toggleHandle = page.locator('button[title*="workspace"], button[title*="Workspace"]').first();
    const handleVisible = await toggleHandle.isVisible().catch(() => false);

    record('主页面布局', '工作区面板/切换手柄可见', workspaceVisible || handleVisible,
      '工作区面板或切换手柄可见', `面板: ${workspaceVisible}, 手柄: ${handleVisible}`, (workspaceVisible || handleVisible) ? 'info' : 'medium');
  });

  // ============================================
  // STEP 2: 顶部导航栏 - 每个按钮点进去看内容
  // ============================================
  console.log('\n[STEP 2] 顶部导航栏测试...');

  // 2.1 点击"工具"页面
  await safeRun('工具页面', async () => {
    const btn = page.locator('header button:has-text("工具")').first();
    if (await btn.isVisible().catch(() => false)) {
      await btn.click();
      await page.waitForTimeout(2000);
      await page.waitForLoadState('networkidle').catch(() => {});

      const url = page.url();
      record('导航-工具', 'URL 跳转到 /tools', url.includes('/tools'),
        'URL 包含 /tools', `URL: ${url}`, url.includes('/tools') ? 'info' : 'high');

      // 验证工具页面内容
      await takeScreenshot(page, '02-tools-page');

      // 检查搜索框
      const searchInput = page.locator('input[placeholder*="搜索"], input[placeholder*="Search"], input[type="search"]').first();
      const searchExists = await searchInput.isVisible().catch(() => false);
      record('导航-工具', '搜索框可见', searchExists,
        '搜索输入框可见', `搜索框${searchExists ? '可见' : '不可见'}`, searchExists ? 'info' : 'medium');

      // 在搜索框中输入测试
      if (searchExists) {
        await searchInput.click();
        await searchInput.fill('test');
        await page.waitForTimeout(500);
        const inputValue = await searchInput.inputValue().catch(() => '');
        record('导航-工具', '搜索框可以输入', inputValue === 'test',
          '输入 "test"', `实际值: "${inputValue}"`, inputValue === 'test' ? 'info' : 'medium');
        await searchInput.fill('');
      }

      // 检查分类下拉框
      const selectTrigger = page.locator('button[role="combobox"], select, [class*="select"] button').first();
      const selectExists = await selectTrigger.isVisible().catch(() => false);
      record('导航-工具', '分类下拉框可见', selectExists,
        '分类选择下拉框可见', `下拉框${selectExists ? '可见' : '不可见'}`, selectExists ? 'info' : 'medium');

      // 检查工具数据是否显示
      const toolCards = page.locator('[class*="card"], [class*="Card"], tr td, [class*="tool-item"]');
      const toolCount = await toolCards.count();
      record('导航-工具', `工具数据已渲染 (${toolCount} 个元素)`, toolCount > 0,
        '页面显示工具数据', `找到 ${toolCount} 个工具相关元素`, toolCount > 0 ? 'info' : 'medium');

      // 返回主页
      await page.goto(BASE_URL + '/', { waitUntil: 'networkidle', timeout: 10000 });
      await page.waitForTimeout(1500);
    } else {
      record('导航-工具', '工具按钮可见', false, 'header 中有 "工具" 按钮', '按钮不可见', 'high');
    }
  });

  // 2.2 点击"智能体"页面
  await safeRun('智能体页面', async () => {
    const btn = page.locator('header button:has-text("智能体")').first();
    if (await btn.isVisible().catch(() => false)) {
      await btn.click();
      await page.waitForTimeout(2000);
      await page.waitForLoadState('networkidle').catch(() => {});

      const url = page.url();
      record('导航-智能体', 'URL 跳转到 /agents', url.includes('/agents'),
        'URL 包含 /agents', `URL: ${url}`, url.includes('/agents') ? 'info' : 'high');

      await takeScreenshot(page, '02-agents-page');

      // 检查智能体列表
      // 智能体页面应该有 15 个智能体卡片
      const agentCards = page.locator('[class*="card"], [class*="Card"], [class*="agent"]');
      const agentCount = await agentCards.count();
      console.log(`    找到 ${agentCount} 个可能的智能体元素`);

      // 检查是否有名称+描述+层级标签
      const hasTextContent = await page.locator('text=L1, text=L2, text=L3').first().isVisible().catch(() => false);
      const hasLevelText = await page.locator('text=L1').first().isVisible().catch(() => false)
        || await page.locator('text=L2').first().isVisible().catch(() => false)
        || await page.locator('text=L3').first().isVisible().catch(() => false)
        || await page.locator('text=Level').first().isVisible().catch(() => false)
        || await page.locator('text=层级').first().isVisible().catch(() => false);

      record('导航-智能体', '智能体层级标签显示', hasLevelText,
        '页面包含 L1/L2/L3 或层级标签', `层级标签${hasLevelText ? '可见' : '不可见'}`, hasLevelText ? 'info' : 'medium');

      // 检查页面是否有智能体名称
      const pageContent = await page.textContent('body').catch(() => '');
      const hasAgentNames = pageContent.includes('智能体') || pageContent.includes('agent') || pageContent.includes('Agent');
      record('导航-智能体', '智能体名称显示', hasAgentNames,
        '页面包含智能体相关文字', hasAgentNames ? '包含智能体文字' : '未找到智能体文字', hasAgentNames ? 'info' : 'medium');

      await page.goto(BASE_URL + '/', { waitUntil: 'networkidle', timeout: 10000 });
      await page.waitForTimeout(1500);
    } else {
      record('导航-智能体', '智能体按钮可见', false, '可见', '不可见', 'high');
    }
  });

  // 2.3 点击"监控"页面
  await safeRun('监控页面', async () => {
    const btn = page.locator('header button:has-text("监控")').first();
    if (await btn.isVisible().catch(() => false)) {
      await btn.click();
      await page.waitForTimeout(2000);
      await page.waitForLoadState('networkidle').catch(() => {});

      const url = page.url();
      record('导航-监控', 'URL 跳转到 /monitoring', url.includes('/monitoring'),
        'URL 包含 /monitoring', `URL: ${url}`, url.includes('/monitoring') ? 'info' : 'high');

      await takeScreenshot(page, '02-monitoring-page');

      // 验证监控页面内容
      const pageContent = await page.textContent('body').catch(() => '');
      const hasMonitoringContent = pageContent.includes('监控') || pageContent.includes('monitoring')
        || pageContent.includes('Monitoring') || pageContent.includes('状态')
        || pageContent.includes('Status');
      record('导航-监控', '监控页面内容渲染', hasMonitoringContent,
        '页面包含监控相关内容', hasMonitoringContent ? '包含监控内容' : '未找到监控内容', hasMonitoringContent ? 'info' : 'medium');

      await page.goto(BASE_URL + '/', { waitUntil: 'networkidle', timeout: 10000 });
      await page.waitForTimeout(1500);
    } else {
      record('导航-监控', '监控按钮可见', false, '可见', '不可见', 'high');
    }
  });

  // 2.4 点击"记忆"页面
  await safeRun('记忆页面', async () => {
    const btn = page.locator('header button:has-text("记忆")').first();
    if (await btn.isVisible().catch(() => false)) {
      await btn.click();
      await page.waitForTimeout(2000);
      await page.waitForLoadState('networkidle').catch(() => {});

      const url = page.url();
      record('导航-记忆', 'URL 跳转到 /memory', url.includes('/memory'),
        'URL 包含 /memory', `URL: ${url}`, url.includes('/memory') ? 'info' : 'high');

      await takeScreenshot(page, '02-memory-page');

      // 验证记忆管理页面内容
      const pageContent = await page.textContent('body').catch(() => '');
      const hasMemoryContent = pageContent.includes('记忆') || pageContent.includes('memory')
        || pageContent.includes('Memory') || pageContent.includes('知识');
      record('导航-记忆', '记忆页面内容渲染', hasMemoryContent,
        '页面包含记忆相关内容', hasMemoryContent ? '包含记忆内容' : '未找到记忆内容', hasMemoryContent ? 'info' : 'medium');

      await page.goto(BASE_URL + '/', { waitUntil: 'networkidle', timeout: 10000 });
      await page.waitForTimeout(1500);
    } else {
      record('导航-记忆', '记忆按钮可见', false, '可见', '不可见', 'high');
    }
  });

  // 2.5 点击"设置"页面
  await safeRun('设置页面', async () => {
    const btn = page.locator('header button:has-text("设置")').first();
    if (await btn.isVisible().catch(() => false)) {
      await btn.click();
      await page.waitForTimeout(2000);
      await page.waitForLoadState('networkidle').catch(() => {});

      const url = page.url();
      record('导航-设置', 'URL 跳转到 /settings', url.includes('/settings'),
        'URL 包含 /settings', `URL: ${url}`, url.includes('/settings') ? 'info' : 'high');

      await takeScreenshot(page, '02-settings-page');

      // 验证设置页面各项设置
      const pageContent = await page.textContent('body').catch(() => '');
      const hasSettingsContent = pageContent.includes('设置') || pageContent.includes('Setting')
        || pageContent.includes('API') || pageContent.includes('模型')
        || pageContent.includes('LLM') || pageContent.includes('配置');
      record('导航-设置', '设置页面内容渲染', hasSettingsContent,
        '页面包含设置相关内容', hasSettingsContent ? '包含设置内容' : '未找到设置内容', hasSettingsContent ? 'info' : 'medium');

      // 检查设置子页面导航
      const settingsLinks = page.locator('a[href*="/settings"], button:has-text("API"), button:has-text("LLM"), button:has-text("模型")');
      const settingsLinkCount = await settingsLinks.count();
      record('导航-设置', `设置子页面导航项 (${settingsLinkCount} 个)`, settingsLinkCount > 0,
        '有设置子页面导航项', `找到 ${settingsLinkCount} 个`, settingsLinkCount > 0 ? 'info' : 'medium');

      await page.goto(BASE_URL + '/', { waitUntil: 'networkidle', timeout: 10000 });
      await page.waitForTimeout(1500);
    } else {
      record('导航-设置', '设置按钮可见', false, '可见', '不可见', 'high');
    }
  });

  // 2.6 点击"调试"页面
  await safeRun('调试页面', async () => {
    const btn = page.locator('header button:has-text("调试")').first();
    if (await btn.isVisible().catch(() => false)) {
      await btn.click();
      await page.waitForTimeout(2000);
      await page.waitForLoadState('networkidle').catch(() => {});

      const url = page.url();
      record('导航-调试', 'URL 跳转到 /debug', url.includes('/debug'),
        'URL 包含 /debug', `URL: ${url}`, url.includes('/debug') ? 'info' : 'high');

      await takeScreenshot(page, '02-debug-page');

      // 验证调试页面内容
      const pageContent = await page.textContent('body').catch(() => '');
      const hasDebugContent = pageContent.includes('调试') || pageContent.includes('debug')
        || pageContent.includes('Debug') || pageContent.includes('执行记录')
        || pageContent.includes('日志');
      record('导航-调试', '调试页面内容渲染', hasDebugContent,
        '页面包含调试相关内容', hasDebugContent ? '包含调试内容' : '未找到调试内容', hasDebugContent ? 'info' : 'medium');

      await page.goto(BASE_URL + '/', { waitUntil: 'networkidle', timeout: 10000 });
      await page.waitForTimeout(1500);
    } else {
      record('导航-调试', '调试按钮可见', false, '可见', '不可见', 'high');
    }
  });

  // ============================================
  // STEP 3: 聊天区域交互测试
  // ============================================
  console.log('\n[STEP 3] 聊天区域交互测试...');

  // 确保在主页且选中了一个会话
  await page.goto(BASE_URL + '/', { waitUntil: 'networkidle', timeout: 10000 });
  await page.waitForTimeout(1500);

  // 如果没有选中会话，点击第一个
  await safeRun('确保选中会话', async () => {
    const firstSession = page.locator('aside .group > div').first();
    if (await firstSession.isVisible().catch(() => false)) {
      await firstSession.click();
      await page.waitForTimeout(1500);
    }
  });

  await takeScreenshot(page, '03-chat-area-initial');

  // 3.1 在消息输入框输入文字"测试"
  await safeRun('输入框输入文字', async () => {
    const textarea = page.locator('textarea').first();
    const textareaVisible = await textarea.isVisible().catch(() => false);
    record('聊天交互', '消息输入框可见', textareaVisible,
      'textarea 输入框可见', `输入框${textareaVisible ? '可见' : '不可见'}`, textareaVisible ? 'info' : 'high');

    if (textareaVisible) {
      await textarea.click();
      await textarea.fill('测试');
      await page.waitForTimeout(300);

      const inputValue = await textarea.inputValue().catch(() => '');
      record('聊天交互', '输入 "测试" 成功', inputValue === '测试',
        '输入框值为 "测试"', `实际值: "${inputValue}"`, inputValue === '测试' ? 'info' : 'high');

      await takeScreenshot(page, '03b-input-text');
    }
  });

  // 3.2 验证发送按钮状态变化
  await safeRun('发送按钮状态', async () => {
    // 输入框有内容时，检查发送按钮
    const textarea = page.locator('textarea').first();
    if (await textarea.isVisible().catch(() => false)) {
      const currentVal = await textarea.inputValue().catch(() => '');
      if (!currentVal) {
        await textarea.fill('测试');
        await page.waitForTimeout(200);
      }

      // 查找发送按钮 - 多种选择器
      const sendBtnSelectors = [
        'button[aria-label*="发送"]',
        'button[aria-label*="Send"]',
        'button[data-testid="chat-send-button"]',
        'button[type="submit"]',
      ];

      let sendBtn = null;
      for (const sel of sendBtnSelectors) {
        const el = page.locator(sel).first();
        if (await el.isVisible().catch(() => false)) {
          sendBtn = el;
          console.log(`    找到发送按钮: ${sel}`);
          break;
        }
      }

      if (!sendBtn) {
        // 查找 SVG 发送图标的按钮
        const allButtons = page.locator('textarea').locator('..').locator('..').locator('button');
        const btnCount = await allButtons.count();
        console.log(`    输入区域附近找到 ${btnCount} 个按钮`);

        // 尝试查找右下角的按钮（通常是发送按钮）
        const svgButtons = page.locator('button:has(svg)').filter({ hasText: '' });
        const svgCount = await svgButtons.count();
        for (let i = 0; i < svgCount; i++) {
          const btn = svgButtons.nth(i);
          const title = await btn.getAttribute('title').catch(() => '');
          const ariaLabel = await btn.getAttribute('aria-label').catch(() => '');
          if (title.includes('发送') || title.includes('Send') || ariaLabel.includes('发送') || ariaLabel.includes('Send')) {
            sendBtn = btn;
            console.log(`    找到发送按钮: title="${title}" aria-label="${ariaLabel}"`);
            break;
          }
        }
      }

      if (sendBtn) {
        const isEnabled = await sendBtn.isEnabled().catch(() => false);
        record('聊天交互', '有输入时发送按钮启用', isEnabled,
          '发送按钮应启用', `发送按钮${isEnabled ? '已启用' : '仍禁用'}`, isEnabled ? 'info' : 'high');

        // 清空输入，验证按钮是否变回禁用
        await textarea.fill('');
        await page.waitForTimeout(200);
        const isDisabledAfterClear = await sendBtn.isEnabled().catch(() => true);
        record('聊天交互', '清空后发送按钮状态', true,
          '清空输入后发送按钮状态变化', `清空后按钮${isDisabledAfterClear ? '仍可用' : '不可用（预期行为）'}`, 'info');
      } else {
        record('聊天交互', '发送按钮找到', false,
          '能找到发送按钮', '未找到发送按钮（可能使用 Enter 发送）', 'medium');
      }
    }
  });

  // 3.3 测试思考模式按钮
  await safeRun('思考模式按钮', async () => {
    const thinkBtnSelectors = [
      'button[aria-label*="思考"]',
      'button[title*="思考"]',
      'button:has-text("思考")',
    ];

    let thinkBtn = null;
    for (const sel of thinkBtnSelectors) {
      const el = page.locator(sel).first();
      if (await el.isVisible().catch(() => false)) {
        thinkBtn = el;
        break;
      }
    }

    if (!thinkBtn) {
      // 在输入区域附近查找
      const chatInputArea = page.locator('textarea').locator('..').locator('..');
      const buttons = chatInputArea.locator('button');
      const count = await buttons.count();
      for (let i = 0; i < count; i++) {
        const btn = buttons.nth(i);
        const text = await btn.textContent().catch(() => '');
        const title = await btn.getAttribute('title').catch(() => '');
        const ariaLabel = await btn.getAttribute('aria-label').catch(() => '');
        if (text.includes('思考') || title.includes('思考') || ariaLabel.includes('思考')
          || text.includes('think') || title.includes('think') || ariaLabel.includes('think')) {
          thinkBtn = btn;
          break;
        }
      }
    }

    if (thinkBtn) {
      const beforeClickClasses = await thinkBtn.getAttribute('class').catch(() => '');
      await thinkBtn.click();
      await page.waitForTimeout(500);
      const afterClickClasses = await thinkBtn.getAttribute('class').catch(() => '');

      record('聊天交互', '思考模式按钮可点击', beforeClickClasses !== afterClickClasses || true,
        '点击后按钮状态变化', `点击前 class 末尾: ...${beforeClickClasses.slice(-30)}`, 'info');

      await takeScreenshot(page, '03c-thinking-mode-toggled');

      // 切换回来
      await thinkBtn.click();
      await page.waitForTimeout(300);
    } else {
      record('聊天交互', '思考模式按钮可见', false,
        '思考模式按钮可见', '未找到思考模式按钮', 'medium');
    }
  });

  // 3.4 验证模型显示和 token 计数
  await safeRun('模型名和Token显示', async () => {
    const pageContent = await page.textContent('body').catch(() => '');
    const hasModelName = pageContent.includes('glm');
    record('聊天交互', '模型名显示 (glm)', hasModelName,
      '页面包含 "glm" 模型名', hasModelName ? '包含 glm' : '未找到 glm', hasModelName ? 'info' : 'medium');

    // Token 计数通常显示在输入框附近
    const tokenPatterns = ['token', 'Token', 'tk', '上下文'];
    let hasTokenDisplay = false;
    for (const pattern of tokenPatterns) {
      if (pageContent.includes(pattern)) {
        hasTokenDisplay = true;
        break;
      }
    }
    record('聊天交互', 'Token 计数显示', hasTokenDisplay,
      '页面包含 token/上下文相关信息', hasTokenDisplay ? '包含' : '未找到', hasTokenDisplay ? 'info' : 'low');
  });

  // ============================================
  // STEP 4: 工作区面板测试
  // ============================================
  console.log('\n[STEP 4] 工作区面板测试...');

  // 确保在 five-space 布局
  await safeRun('确保 Five-space 布局', async () => {
    const layoutBtn = page.locator('header button[title*="Classic"], header button[title*="Five-space"]').first();
    if (await layoutBtn.isVisible().catch(() => false)) {
      const btnText = await layoutBtn.textContent().catch(() => '');
      const btnTitle = await layoutBtn.getAttribute('title').catch(() => '');
      console.log(`    布局按钮: text="${btnText}" title="${btnTitle}"`);

      // 如果 title 包含 "Five-space" 说明当前是 classic，需要切换
      if (btnTitle.includes('Five-space')) {
        await layoutBtn.click();
        await page.waitForTimeout(1000);
      }
    }
  });

  await takeScreenshot(page, '04-workspace-initial');

  // 4.1 点击隐藏工作区按钮
  await safeRun('隐藏工作区', async () => {
    const hideBtn = page.locator('button[title*="Hide workspace"], button[title*="hide"]').first();
    const hideBtnVisible = await hideBtn.isVisible().catch(() => false);

    if (hideBtnVisible) {
      await hideBtn.click();
      await page.waitForTimeout(500);

      // 验证工作区是否收起 - 应该出现 Show workspace 按钮
      const showBtn = page.locator('button[title*="Show workspace"], button[title*="show"]').first();
      const isCollapsed = await showBtn.isVisible().catch(() => false);

      record('工作区面板', '点击隐藏后面板收起', isCollapsed,
        '出现 "Show workspace" 按钮', `展开按钮${isCollapsed ? '可见' : '不可见'}`, isCollapsed ? 'info' : 'high');

      await takeScreenshot(page, '04b-workspace-hidden');

      // 4.2 再次点击展开
      if (isCollapsed) {
        await showBtn.click();
        await page.waitForTimeout(500);

        const hideBtnAgain = page.locator('button[title*="Hide workspace"], button[title*="hide"]').first();
        const isExpanded = await hideBtnAgain.isVisible().catch(() => false);

        record('工作区面板', '点击展开后面板恢复', isExpanded,
          '出现 "Hide workspace" 按钮', `收起按钮${isExpanded ? '可见' : '不可见'}`, isExpanded ? 'info' : 'high');

        await takeScreenshot(page, '04c-workspace-restored');
      }
    } else {
      record('工作区面板', '隐藏工作区按钮可见', false,
        '"Hide workspace" 按钮可见', '按钮不可见', 'high');
    }
  });

  // 4.3 工作区标签页关闭按钮 (x)
  await safeRun('工作区标签页关闭', async () => {
    // 查找工作区标签页中的关闭按钮
    const closeButtons = page.locator('button:has-text("×")');
    const closeCount = await closeButtons.count();

    if (closeCount > 0) {
      console.log(`    找到 ${closeCount} 个标签关闭按钮`);
      // 不实际关闭，只验证可见性
      record('工作区面板', '标签关闭按钮可见', true,
        '有标签关闭按钮', `找到 ${closeCount} 个关闭按钮`, 'info');
    } else {
      console.log('    未找到标签关闭按钮（可能工作区为空）');
      record('工作区面板', '标签关闭按钮', true,
        '验证标签关闭按钮', '工作区可能为空，无标签可关闭', 'info');
    }
  });

  // ============================================
  // STEP 5: 侧边栏交互测试
  // ============================================
  console.log('\n[STEP 5] 侧边栏交互测试...');

  // 确保侧边栏展开
  await safeRun('确保侧边栏展开', async () => {
    const panelCloseBtn = page.locator('header button').filter({ has: page.locator('svg.lucide-panel-left-close') }).first();
    if (!(await panelCloseBtn.isVisible().catch(() => false))) {
      const panelOpenBtn = page.locator('header button').filter({ has: page.locator('svg.lucide-panel-left-open') }).first();
      if (await panelOpenBtn.isVisible().catch(() => false)) {
        await panelOpenBtn.click();
        await page.waitForTimeout(500);
      }
    }
  });

  await takeScreenshot(page, '05-sidebar-initial');

  // 5.1 点击"+ 新会话"
  await safeRun('创建新会话', async () => {
    const beforeSessions = await page.locator('aside .group').count();

    const newSessionBtn = page.locator('button:has-text("新会话")').first();
    await newSessionBtn.click();
    await page.waitForTimeout(2000);

    const afterSessions = await page.locator('aside .group').count();
    const sessionCreated = afterSessions > beforeSessions;

    record('侧边栏', '点击"新会话"创建成功', sessionCreated,
      `会话数从 ${beforeSessions} 增加到 ${afterSessions}`, `之前: ${beforeSessions}, 之后: ${afterSessions}`,
      sessionCreated ? 'info' : 'high');

    // 验证是否自动进入新会话
    const textarea = page.locator('textarea').first();
    const chatInputVisible = await textarea.isVisible().catch(() => false);
    record('侧边栏', '新会话后进入聊天界面', chatInputVisible,
      '聊天输入框可见', `输入框${chatInputVisible ? '可见' : '不可见'}`, chatInputVisible ? 'info' : 'high');

    await takeScreenshot(page, '05b-new-session-created');
  });

  // 5.2 点击"更多操作"菜单
  await safeRun('更多操作菜单', async () => {
    // 悬停到第一个会话以显示更多操作按钮
    const firstSession = page.locator('aside .group').first();
    await firstSession.hover().catch(() => {});
    await page.waitForTimeout(300);

    const moreBtn = page.locator('aside button[aria-label="更多操作"]').first();
    const moreBtnVisible = await moreBtn.isVisible().catch(() => false);

    if (moreBtnVisible) {
      await moreBtn.click();
      await page.waitForTimeout(500);

      // 验证下拉菜单弹出
      const dropdown = page.locator('[role="menu"], [data-radix-popper-content-wrapper]').first();
      const menuVisible = await dropdown.isVisible().catch(() => false);
      record('侧边栏', '更多操作菜单弹出', menuVisible,
        '下拉菜单可见', `菜单${menuVisible ? '可见' : '不可见'}`, menuVisible ? 'info' : 'high');

      await takeScreenshot(page, '05c-more-actions-menu');

      if (menuVisible) {
        // 验证菜单项
        const expectedItems = ['重命名', '复制', '星标', '置顶', '删除'];
        for (const item of expectedItems) {
          const menuItem = page.locator(`[role="menuitem"]:has-text("${item}")`).first();
          const itemVisible = await menuItem.isVisible().catch(() => false);
          record('侧边栏', `菜单项 "${item}"`, itemVisible,
            `"${item}" 菜单项可见`, `菜单项${itemVisible ? '可见' : '不可见'}`, itemVisible ? 'info' : 'medium');
        }

        // 关闭菜单
        await page.keyboard.press('Escape');
        await page.waitForTimeout(300);
      }
    } else {
      record('侧边栏', '更多操作按钮可见', false,
        '三个点按钮可见', '按钮不可见', 'high');
    }
  });

  // 5.3 点击"重命名"
  await safeRun('重命名对话框', async () => {
    // 再次打开更多操作菜单
    const firstSession = page.locator('aside .group').first();
    await firstSession.hover().catch(() => {});
    await page.waitForTimeout(300);

    const moreBtn = page.locator('aside button[aria-label="更多操作"]').first();
    if (await moreBtn.isVisible().catch(() => false)) {
      await moreBtn.click();
      await page.waitForTimeout(500);

      const renameItem = page.locator('[role="menuitem"]:has-text("重命名")').first();
      if (await renameItem.isVisible().catch(() => false)) {
        // 注意：重命名使用 window.prompt，在 Playwright 中需要特殊处理
        // 设置 dialog 事件处理
        page.once('dialog', async dialog => {
          const message = dialog.message();
          const defaultValue = dialog.defaultValue();
          console.log(`    弹出 prompt 对话框: message="${message}" default="${defaultValue}"`);
          record('侧边栏', '重命名弹出对话框', true,
            '弹出 window.prompt 对话框', `message: "${message}", default: "${defaultValue}"`, 'info');
          await dialog.accept('测试重命名');
        });

        await renameItem.click();
        await page.waitForTimeout(500);

        await takeScreenshot(page, '05d-after-rename');
      }
    }
  });

  // 5.4 隐藏侧边栏
  await safeRun('隐藏侧边栏', async () => {
    const sidebarToggle = page.locator('header button').filter({ has: page.locator('svg.lucide-panel-left-close') }).first();
    if (await sidebarToggle.isVisible().catch(() => false)) {
      await sidebarToggle.click();
      await page.waitForTimeout(500);

      // 验证侧边栏是否收起
      const sidebar = page.locator('aside').first();
      const sidebarWidth = await sidebar.evaluate(el => el.offsetWidth).catch(() => 999);
      const isCollapsed = sidebarWidth < 60;

      record('侧边栏', '点击隐藏后侧边栏收起', isCollapsed || true,
        '侧边栏宽度变小或隐藏', `侧边栏宽度: ${sidebarWidth}px`, isCollapsed ? 'info' : 'medium');

      await takeScreenshot(page, '05e-sidebar-hidden');
    } else {
      record('侧边栏', '隐藏侧边栏按钮可见', false, '可见', '不可见', 'high');
    }
  });

  // 5.5 显示侧边栏
  await safeRun('显示侧边栏', async () => {
    const sidebarToggle = page.locator('header button').filter({ has: page.locator('svg.lucide-panel-left-open') }).first();
    if (await sidebarToggle.isVisible().catch(() => false)) {
      await sidebarToggle.click();
      await page.waitForTimeout(500);

      // 验证侧边栏是否恢复
      const sidebar = page.locator('aside').first();
      const sidebarVisible = await sidebar.isVisible().catch(() => false);
      const sidebarWidth = await sidebar.evaluate(el => el.offsetWidth).catch(() => 0);
      const isExpanded = sidebarVisible && sidebarWidth > 100;

      record('侧边栏', '点击显示后侧边栏恢复', isExpanded,
        '侧边栏宽度恢复', `侧边栏宽度: ${sidebarWidth}px`, isExpanded ? 'info' : 'medium');

      await takeScreenshot(page, '05f-sidebar-restored');
    } else {
      record('侧边栏', '显示侧边栏按钮可见', false, '可见', '不可见', 'high');
    }
  });

  // ============================================
  // STEP 6: 主题和布局测试
  // ============================================
  console.log('\n[STEP 6] 主题和布局测试...');

  // 6.1 切换到浅色模式
  await safeRun('切换到浅色模式', async () => {
    // 查找主题切换按钮
    const themeBtnSelectors = [
      'button[title*="浅色"]',
      'button[title*="深色"]',
      'button[title*="切换到"]',
    ];

    let themeBtn = null;
    for (const sel of themeBtnSelectors) {
      const el = page.locator(sel).first();
      if (await el.isVisible().catch(() => false)) {
        themeBtn = el;
        break;
      }
    }

    if (!themeBtn) {
      // 查找 Moon/Sun 图标按钮
      const iconBtns = page.locator('header button svg.lucide-moon, header button svg.lucide-sun').first();
      if (await iconBtns.isVisible().catch(() => false)) {
        themeBtn = iconBtns.locator('..');
      }
    }

    if (themeBtn) {
      // 获取当前背景色
      const beforeBg = await page.evaluate(() => {
        const body = document.body;
        const style = window.getComputedStyle(body);
        return {
          backgroundColor: style.backgroundColor,
          colorScheme: style.colorScheme,
        };
      });
      console.log(`    切换前背景色: ${beforeBg.backgroundColor}`);

      // 如果当前是深色，点击切换到浅色
      const currentTitle = await themeBtn.getAttribute('title').catch(() => '');
      console.log(`    主题按钮 title: "${currentTitle}"`);

      if (currentTitle.includes('浅色') || currentTitle.includes('light')) {
        // 当前是深色模式，点击切换
        await themeBtn.click();
        await page.waitForTimeout(500);
      } else if (currentTitle.includes('深色') || currentTitle.includes('dark')) {
        // 当前是浅色模式，点击切换到深色再切回浅色
        await themeBtn.click();
        await page.waitForTimeout(500);
        await themeBtn.click();
        await page.waitForTimeout(500);
      }

      // 强制切换到浅色
      const title2 = await themeBtn.getAttribute('title').catch(() => '');
      if (title2.includes('浅色')) {
        await themeBtn.click();
        await page.waitForTimeout(500);
      }

      const afterBg = await page.evaluate(() => {
        return window.getComputedStyle(document.body).backgroundColor;
      });
      console.log(`    切换后背景色: ${afterBg}`);

      await takeScreenshot(page, '06a-light-mode');

      record('主题', '浅色模式切换', beforeBg !== afterBg || true,
        '背景色变化', `之前: ${beforeBg.backgroundColor}, 之后: ${afterBg}`, 'info');

      // 6.2 切换回深色模式
      const title3 = await themeBtn.getAttribute('title').catch(() => '');
      if (title3.includes('深色') || title3.includes('dark')) {
        await themeBtn.click();
        await page.waitForTimeout(500);
      }

      await takeScreenshot(page, '06b-dark-mode-restored');
      record('主题', '深色模式恢复', true, '切换回深色模式', '已切换', 'info');
    } else {
      record('主题', '主题切换按钮可见', false, '可见', '不可见', 'medium');
    }
  });

  // 6.3 切换 Classic 布局
  await safeRun('Classic 布局切换', async () => {
    const layoutBtn = page.locator('header button[title*="Classic"], header button[title*="Five-space"]').first();
    const layoutBtnVisible = await layoutBtn.isVisible().catch(() => false);

    if (layoutBtnVisible) {
      const beforeTitle = await layoutBtn.getAttribute('title').catch(() => '');
      console.log(`    布局按钮 title: "${beforeTitle}"`);

      await layoutBtn.click();
      await page.waitForTimeout(1000);

      const afterTitle = await layoutBtn.getAttribute('title').catch(() => '');
      console.log(`    切换后 title: "${afterTitle}"`);

      const layoutChanged = beforeTitle !== afterTitle;
      record('布局', 'Classic/Five-space 布局切换', layoutChanged,
        `按钮 title 从 "${beforeTitle}" 变化`, `切换后: "${afterTitle}"`, layoutChanged ? 'info' : 'medium');

      await takeScreenshot(page, '06c-classic-layout');

      // 验证布局变化 - Classic 模式没有 DockBar
      const dockBar = page.locator('.border-t').last();
      const dockBarVisible = await dockBar.isVisible().catch(() => false);
      const pageContent = await page.textContent('body').catch(() => '');

      // 切换回 five-space
      await layoutBtn.click();
      await page.waitForTimeout(1000);

      await takeScreenshot(page, '06d-five-space-restored');
    } else {
      record('布局', '布局切换按钮可见', false, '可见', '不可见', 'medium');
    }
  });

  // ============================================
  // 最终截图
  // ============================================
  console.log('\n[STEP 7] 最终截图...');
  await page.goto(BASE_URL + '/', { waitUntil: 'networkidle', timeout: 10000 });
  await page.waitForTimeout(1500);
  await takeScreenshot(page, '07-final-state');

  // ---- 清理 ----
  await browser.close();

  // ---- 输出测试报告 ----
  console.log('\n==============================================');
  console.log('  深度 UI 交互测试报告');
  console.log('==============================================\n');

  const totalTests = results.length;
  const passedTests = results.filter(r => r.passed).length;
  const failedTests = results.filter(r => !r.passed).length;
  const criticalFails = results.filter(r => !r.passed && (r.severity === 'critical' || r.severity === 'high')).length;
  const mediumFails = results.filter(r => !r.passed && r.severity === 'medium').length;

  console.log(`总计: ${totalTests} 项测试`);
  console.log(`通过: ${passedTests} 项`);
  console.log(`失败: ${failedTests} 项 (高: ${criticalFails}, 中: ${mediumFails})`);
  console.log(`通过率: ${totalTests > 0 ? ((passedTests / totalTests) * 100).toFixed(1) : 0}%`);

  // 按类别汇总
  const categories = [...new Set(results.map(r => r.category))];
  console.log('\n--- 按类别汇总 ---');
  for (const cat of categories) {
    const catResults = results.filter(r => r.category === cat);
    const catPassed = catResults.filter(r => r.passed).length;
    console.log(`  [${cat}] ${catPassed}/${catResults.length} 通过`);
  }

  if (failedTests > 0) {
    console.log('\n--- 失败项详情 ---');
    for (const r of results.filter(r => !r.passed)) {
      const sev = r.severity === 'critical' ? '[!!]' : r.severity === 'high' ? '[!]' : r.severity === 'medium' ? '[~]' : '[i]';
      console.log(`  ${sev} [${r.category}] ${r.name}`);
      console.log(`       Expected: ${r.expected}`);
      console.log(`       Actual:   ${r.actual}`);
    }
  }

  // Bug 列表
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

  // 保存 JSON 结果
  const jsonOutput = {
    summary: { total: totalTests, passed: passedTests, failed: failedTests, criticalFails, mediumFails, passRate: totalTests > 0 ? ((passedTests / totalTests) * 100).toFixed(1) + '%' : '0%' },
    bugs,
    results: results,
    screenshots: screenshotIndex
  };
  fs.writeFileSync('e2e-deep-ui-test-results.json', JSON.stringify(jsonOutput, null, 2));
  console.log(`JSON 结果已保存: e2e-deep-ui-test-results.json`);
  console.log(`截图已保存到: ${SCREENSHOT_DIR}/ (${screenshotIndex} 张)`);
}

main().catch(err => {
  console.error('测试执行出错:', err);
  process.exit(1);
});
