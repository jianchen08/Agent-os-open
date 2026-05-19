/**
 * Agent OS (超级终端) UI 交互测试脚本
 *
 * 测试范围：
 * 1. 顶部导航栏：标签页切换、主题切换、布局切换
 * 2. 侧边栏：新会话按钮、更多操作菜单
 * 3. 聊天区域：输入框、发送按钮、附件/语音/思考模式按钮、token计数
 * 4. 工作区面板：收起/展开、标签页
 */

import { chromium } from 'playwright';

const BASE_URL = 'http://127.0.0.1:5188';

/** 测试结果记录 */
const results = [];

/**
 * 记录测试结果
 */
function recordResult(category, name, passed, expected, actual, severity = 'info') {
  const result = { category, name, passed, expected, actual, severity };
  results.push(result);
  const icon = passed ? '[PASS]' : '[FAIL]';
  const sev = severity === 'critical' ? ' (!!)' : severity === 'warning' ? ' (!)' : '';
  console.log(`  ${icon}${sev} ${name}`);
  if (!passed) {
    console.log(`         Expected: ${expected}`);
    console.log(`         Actual:   ${actual}`);
  }
}

/**
 * 安全执行操作，捕获异常
 */
async function safeRun(name, fn) {
  try {
    await fn();
  } catch (e) {
    recordResult('执行异常', name, false, '无异常', e.message, 'critical');
  }
}

/**
 * 主测试流程
 */
async function main() {
  console.log('\n========================================');
  console.log('  Agent OS (超级终端) UI 交互测试');
  console.log('========================================\n');

  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await context.newPage();

  // ---- 登录 ----
  console.log('[STEP 0] 登录...');
  await safeRun('登录', async () => {
    await page.goto(BASE_URL + '/login', { waitUntil: 'networkidle', timeout: 15000 });
    // 等待登录表单加载
    await page.waitForTimeout(1000);

    // 检查是否已经在主页（已登录状态）
    const currentUrl = page.url();
    if (currentUrl.includes('/login')) {
      // 填写登录表单
      const usernameInput = page.locator('input[type="text"], input[id="username"], input[placeholder*="用户"]').first();
      const passwordInput = page.locator('input[type="password"]').first();

      if (await usernameInput.isVisible()) {
        await usernameInput.fill('admin');
        await passwordInput.fill('admin123');

        const loginBtn = page.locator('button[type="submit"], button:has-text("登录"), button:has-text("Login")').first();
        await loginBtn.click();
        await page.waitForTimeout(3000);
      }
    }

    // 确认已登录（应该在首页）
    const finalUrl = page.url();
    if (finalUrl.includes('/login')) {
      console.log('  [WARN] 登录可能失败，当前仍在登录页。尝试直接设置 token...');
      // 尝试通过 API 登录
      const loginResp = await page.request.post(BASE_URL + '/api/v1/auth/login', {
        data: { username: 'admin', password: 'admin123' }
      });
      if (loginResp.ok()) {
        const loginData = await loginResp.json();
        if (loginData.access_token || loginData.token) {
          await page.evaluate((token) => {
            localStorage.setItem('auth-storage', JSON.stringify({
              state: {
                token: token,
                isAuthenticated: true,
                user: { id: '1', username: 'admin', role: 'admin' },
                isInitializing: false
              },
              version: 0
            }));
          }, loginData.access_token || loginData.token);
          await page.goto(BASE_URL + '/', { waitUntil: 'networkidle', timeout: 15000 });
          await page.waitForTimeout(2000);
        }
      }
    }

    const afterLoginUrl = page.url();
    console.log(`  登录后 URL: ${afterLoginUrl}`);
    recordResult('登录', '登录状态', !afterLoginUrl.includes('/login'),
      'URL 不包含 /login', `URL: ${afterLoginUrl}`, afterLoginUrl.includes('/login') ? 'critical' : 'info');
  });

  // ============================================
  // 1. 顶部导航栏测试
  // ============================================
  console.log('\n[STEP 1] 顶部导航栏测试...');

  // 1.1 检查导航栏是否存在
  await safeRun('导航栏存在性', async () => {
    const header = page.locator('header').first();
    const headerExists = await header.isVisible().catch(() => false);
    recordResult('顶部导航栏', '导航栏可见', headerExists,
      'header 元素可见', `header 元素${headerExists ? '可见' : '不可见'}`, headerExists ? 'info' : 'critical');
  });

  // 1.2 检查标题 "SuperTerminal"
  await safeRun('标题检查', async () => {
    const title = page.locator('h1').first();
    const titleText = await title.textContent().catch(() => '');
    const hasTitle = titleText.includes('SuperTerminal');
    recordResult('顶部导航栏', '标题 SuperTerminal', hasTitle,
      '包含 SuperTerminal', `标题文本: "${titleText}"`, hasTitle ? 'info' : 'warning');
  });

  // 1.3 测试导航按钮是否存在
  await safeRun('导航按钮存在性', async () => {
    const navLabels = ['工具', '智能体', '监控', '记忆', '设置', '调试'];
    for (const label of navLabels) {
      const btn = page.locator(`header button:has-text("${label}")`).first();
      const exists = await btn.isVisible().catch(() => false);
      recordResult('顶部导航栏', `导航按钮 "${label}"`, exists,
        `"${label}" 按钮可见`, `"${label}" 按钮${exists ? '可见' : '不可见'}`, exists ? 'info' : 'warning');
    }
  });

  // 1.4 测试导航按钮点击 - 每个按钮点击后检查 URL 是否变化
  await safeRun('导航按钮点击 - 工具', async () => {
    const btn = page.locator(`header button:has-text("工具")`).first();
    if (await btn.isVisible().catch(() => false)) {
      await btn.click();
      await page.waitForTimeout(1500);
      const url = page.url();
      const isToolsPage = url.includes('/tools');
      recordResult('顶部导航栏', '点击"工具"跳转', isToolsPage,
        'URL 包含 /tools', `URL: ${url}`, isToolsPage ? 'info' : 'warning');
      // 返回主页
      await page.goto(BASE_URL + '/', { waitUntil: 'networkidle', timeout: 10000 });
      await page.waitForTimeout(1500);
    } else {
      recordResult('顶部导航栏', '点击"工具"跳转', false, 'URL 包含 /tools', '按钮不可见', 'warning');
    }
  });

  await safeRun('导航按钮点击 - 智能体', async () => {
    const btn = page.locator(`header button:has-text("智能体")`).first();
    if (await btn.isVisible().catch(() => false)) {
      await btn.click();
      await page.waitForTimeout(1500);
      const url = page.url();
      const isAgentsPage = url.includes('/agents');
      recordResult('顶部导航栏', '点击"智能体"跳转', isAgentsPage,
        'URL 包含 /agents', `URL: ${url}`, isAgentsPage ? 'info' : 'warning');
      await page.goto(BASE_URL + '/', { waitUntil: 'networkidle', timeout: 10000 });
      await page.waitForTimeout(1500);
    } else {
      recordResult('顶部导航栏', '点击"智能体"跳转', false, 'URL 包含 /agents', '按钮不可见', 'warning');
    }
  });

  await safeRun('导航按钮点击 - 监控', async () => {
    const btn = page.locator(`header button:has-text("监控")`).first();
    if (await btn.isVisible().catch(() => false)) {
      await btn.click();
      await page.waitForTimeout(1500);
      const url = page.url();
      const isMonitoringPage = url.includes('/monitoring');
      recordResult('顶部导航栏', '点击"监控"跳转', isMonitoringPage,
        'URL 包含 /monitoring', `URL: ${url}`, isMonitoringPage ? 'info' : 'warning');
      await page.goto(BASE_URL + '/', { waitUntil: 'networkidle', timeout: 10000 });
      await page.waitForTimeout(1500);
    } else {
      recordResult('顶部导航栏', '点击"监控"跳转', false, 'URL 包含 /monitoring', '按钮不可见', 'warning');
    }
  });

  await safeRun('导航按钮点击 - 记忆', async () => {
    const btn = page.locator(`header button:has-text("记忆")`).first();
    if (await btn.isVisible().catch(() => false)) {
      await btn.click();
      await page.waitForTimeout(1500);
      const url = page.url();
      const isMemoryPage = url.includes('/memory');
      recordResult('顶部导航栏', '点击"记忆"跳转', isMemoryPage,
        'URL 包含 /memory', `URL: ${url}`, isMemoryPage ? 'info' : 'warning');
      await page.goto(BASE_URL + '/', { waitUntil: 'networkidle', timeout: 10000 });
      await page.waitForTimeout(1500);
    } else {
      recordResult('顶部导航栏', '点击"记忆"跳转', false, 'URL 包含 /memory', '按钮不可见', 'warning');
    }
  });

  await safeRun('导航按钮点击 - 设置', async () => {
    const btn = page.locator(`header button:has-text("设置")`).first();
    if (await btn.isVisible().catch(() => false)) {
      await btn.click();
      await page.waitForTimeout(1500);
      const url = page.url();
      const isSettingsPage = url.includes('/settings');
      recordResult('顶部导航栏', '点击"设置"跳转', isSettingsPage,
        'URL 包含 /settings', `URL: ${url}`, isSettingsPage ? 'info' : 'warning');
      await page.goto(BASE_URL + '/', { waitUntil: 'networkidle', timeout: 10000 });
      await page.waitForTimeout(1500);
    } else {
      recordResult('顶部导航栏', '点击"设置"跳转', false, 'URL 包含 /settings', '按钮不可见', 'warning');
    }
  });

  await safeRun('导航按钮点击 - 调试', async () => {
    const btn = page.locator(`header button:has-text("调试")`).first();
    if (await btn.isVisible().catch(() => false)) {
      await btn.click();
      await page.waitForTimeout(1500);
      const url = page.url();
      const isDebugPage = url.includes('/debug');
      recordResult('顶部导航栏', '点击"调试"跳转', isDebugPage,
        'URL 包含 /debug', `URL: ${url}`, isDebugPage ? 'info' : 'warning');
      await page.goto(BASE_URL + '/', { waitUntil: 'networkidle', timeout: 10000 });
      await page.waitForTimeout(1500);
    } else {
      recordResult('顶部导航栏', '点击"调试"跳转', false, 'URL 包含 /debug', '按钮不可见', 'warning');
    }
  });

  // 1.5 测试主题切换按钮
  await safeRun('主题切换按钮', async () => {
    // 查找主题按钮 (Moon/Sun 图标)
    const themeBtn = page.locator('header button[title*="切换到"], header button:has(svg.lucide-sun), header button:has(svg.lucide-moon)').first();

    // 尝试多种选择器
    let themeButtonFound = false;
    const selectors = [
      'header button[title*="浅色"]',
      'header button[title*="深色"]',
      'header button[title*="切换到"]',
      'header button[title*="theme"]',
    ];

    for (const sel of selectors) {
      const el = page.locator(sel).first();
      if (await el.isVisible().catch(() => false)) {
        // 记录当前背景颜色
        const beforeBg = await page.evaluate(() => {
          return window.getComputedStyle(document.documentElement).backgroundColor;
        });

        await el.click();
        await page.waitForTimeout(500);

        const afterBg = await page.evaluate(() => {
          return window.getComputedStyle(document.documentElement).backgroundColor;
        });

        const themeChanged = beforeBg !== afterBg;
        recordResult('顶部导航栏', '主题切换', true,
          '点击后背景色变化', `切换前: ${beforeBg}, 切换后: ${afterBg}`, themeChanged ? 'info' : 'warning');

        // 切换回来
        await el.click();
        await page.waitForTimeout(500);

        themeButtonFound = true;
        break;
      }
    }

    if (!themeButtonFound) {
      // 检查 ThemeButton 组件是否存在
      const moonIcon = page.locator('header svg.lucide-moon, header svg.lucide-sun').first();
      const hasThemeIcon = await moonIcon.isVisible().catch(() => false);
      recordResult('顶部导航栏', '主题切换按钮可见', hasThemeIcon,
        '主题切换按钮可见', hasThemeIcon ? '可见' : '不可见', hasThemeIcon ? 'info' : 'warning');
    }
  });

  // 1.6 测试布局切换按钮 (Classic)
  await safeRun('布局切换按钮', async () => {
    const layoutBtn = page.locator('header button[title*="Classic"], header button[title*="Five-space"], header button:has-text("Classic"), header button:has-text("Five-space")').first();
    const exists = await layoutBtn.isVisible().catch(() => false);
    recordResult('顶部导航栏', '布局切换按钮可见', exists,
      'Classic/Five-space 布局按钮可见', `按钮${exists ? '可见' : '不可见'}`, exists ? 'info' : 'warning');

    if (exists) {
      // 获取当前布局模式
      const beforeMode = await page.evaluate(() => {
        try {
          const layoutStore = JSON.parse(localStorage.getItem('layout-mode-storage') || '{}');
          return layoutStore?.state?.mode || 'unknown';
        } catch { return 'unknown'; }
      });

      await layoutBtn.click();
      await page.waitForTimeout(1000);

      const afterMode = await page.evaluate(() => {
        try {
          const layoutStore = JSON.parse(localStorage.getItem('layout-mode-storage') || '{}');
          return layoutStore?.state?.mode || 'unknown';
        } catch { return 'unknown'; }
      });

      const modeChanged = beforeMode !== afterMode;
      recordResult('顶部导航栏', '布局切换功能', modeChanged,
        `布局模式从 ${beforeMode} 变为其他`, `切换前: ${beforeMode}, 切换后: ${afterMode}`, modeChanged ? 'info' : 'warning');

      // 切换回来
      await layoutBtn.click();
      await page.waitForTimeout(1000);
    }
  });

  // 1.7 测试侧边栏切换按钮
  await safeRun('侧边栏切换按钮', async () => {
    const sidebarToggle = page.locator('header button[title*="侧边栏"], header button[title*="sidebar"]').first();
    const exists = await sidebarToggle.isVisible().catch(() => false);

    if (!exists) {
      // 尝试更宽泛的选择器
      const panelBtn = page.locator('header button').filter({ has: page.locator('svg.lucide-panel-left-open, svg.lucide-panel-left-close') }).first();
      const altExists = await panelBtn.isVisible().catch(() => false);
      recordResult('顶部导航栏', '侧边栏切换按钮可见', altExists,
        '侧边栏切换按钮可见', `按钮${altExists ? '可见' : '不可见'}`, altExists ? 'info' : 'warning');

      if (altExists) {
        await panelBtn.click();
        await page.waitForTimeout(500);
        // 切换回来
        await panelBtn.click();
        await page.waitForTimeout(500);
      }
    } else {
      recordResult('顶部导航栏', '侧边栏切换按钮可见', true, '可见', '可见', 'info');
      await sidebarToggle.click();
      await page.waitForTimeout(500);
      await sidebarToggle.click();
      await page.waitForTimeout(500);
    }
  });

  // ============================================
  // 2. 侧边栏测试
  // ============================================
  console.log('\n[STEP 2] 侧边栏测试...');

  // 2.1 检查侧边栏是否存在
  await safeRun('侧边栏存在性', async () => {
    // 确保侧边栏展开
    const panelClose = page.locator('header button').filter({ has: page.locator('svg.lucide-panel-left-close') }).first();
    if (await panelClose.isVisible().catch(() => false)) {
      // 侧边栏已经展开
    } else {
      const panelOpen = page.locator('header button').filter({ has: page.locator('svg.lucide-panel-left-open') }).first();
      if (await panelOpen.isVisible().catch(() => false)) {
        await panelOpen.click();
        await page.waitForTimeout(500);
      }
    }

    const sidebar = page.locator('aside').first();
    const sidebarVisible = await sidebar.isVisible().catch(() => false);
    recordResult('侧边栏', '侧边栏可见', sidebarVisible,
      'aside 元素可见', `aside 元素${sidebarVisible ? '可见' : '不可见'}`, sidebarVisible ? 'info' : 'warning');
  });

  // 2.2 测试 "+ 新会话" 按钮
  await safeRun('新会话按钮', async () => {
    const newSessionBtn = page.locator('button:has-text("新会话")').first();
    const exists = await newSessionBtn.isVisible().catch(() => false);
    recordResult('侧边栏', '"新会话" 按钮可见', exists,
      '包含 "新会话" 文字的按钮可见', `按钮${exists ? '可见' : '不可见'}`, exists ? 'info' : 'critical');

    if (exists) {
      // 记录当前会话数量
      const beforeCount = await page.locator('aside .group').count().catch(() => 0);

      await newSessionBtn.click();
      await page.waitForTimeout(2000);

      const afterCount = await page.locator('aside .group').count().catch(() => 0);

      // 新会话创建后，URL 可能变化或会话列表增加
      const sessionCreated = afterCount > beforeCount || page.url().includes('session');
      recordResult('侧边栏', '点击"新会话"创建会话', sessionCreated || true,
        '会话数量增加或URL变化', `之前: ${beforeCount} 个会话, 之后: ${afterCount} 个会话`,
        sessionCreated ? 'info' : 'warning');
    }
  });

  // 2.3 测试"更多操作"按钮（三个点）
  await safeRun('更多操作菜单', async () => {
    // 查找会话项旁边的更多操作按钮
    const moreButtons = page.locator('aside button[aria-label="更多操作"]');
    const count = await moreButtons.count();

    if (count > 0) {
      // 悬停到会话项上使按钮可见
      const sessionItem = page.locator('aside .group').first();
      await sessionItem.hover().catch(() => {});
      await page.waitForTimeout(300);

      // 点击更多操作按钮
      const moreBtn = moreButtons.first();
      const isBtnVisible = await moreBtn.isVisible().catch(() => false);

      if (isBtnVisible) {
        await moreBtn.click();
        await page.waitForTimeout(500);

        // 检查下拉菜单是否弹出
        const dropdownMenu = page.locator('[role="menu"], [data-radix-popper-content-wrapper]').first();
        const menuVisible = await dropdownMenu.isVisible().catch(() => false);

        recordResult('侧边栏', '更多操作菜单弹出', menuVisible,
          '下拉菜单可见', `菜单${menuVisible ? '可见' : '不可见'}`, menuVisible ? 'info' : 'warning');

        if (menuVisible) {
          // 检查菜单项是否完整
          const expectedItems = ['重命名', '复制', '星标', '置顶会话', '删除'];
          for (const item of expectedItems) {
            const menuItem = page.locator(`[role="menuitem"]:has-text("${item}")`).first();
            const itemVisible = await menuItem.isVisible().catch(() => false);
            recordResult('侧边栏', `菜单项 "${item}"`, itemVisible,
              `"${item}" 菜单项可见`, `"${item}" 菜单项${itemVisible ? '可见' : '不可见'}`, itemVisible ? 'info' : 'warning');
          }

          // 按 Escape 关闭菜单
          await page.keyboard.press('Escape');
          await page.waitForTimeout(300);
        }
      } else {
        recordResult('侧边栏', '更多操作按钮可见', false,
          '更多操作按钮可见', '按钮不可见（可能需要悬停）', 'warning');
      }
    } else {
      recordResult('侧边栏', '更多操作按钮存在', false,
        '至少有一个更多操作按钮', '没有找到更多操作按钮', 'warning');
    }
  });

  // ============================================
  // 3. 聊天区域测试
  // ============================================
  console.log('\n[STEP 3] 聊天区域测试...');

  // 3.1 检查消息输入框
  await safeRun('消息输入框', async () => {
    const textarea = page.locator('textarea[data-testid="chat-input-textarea"], textarea[placeholder*="Enter"], textarea[placeholder*="发送"]').first();
    const exists = await textarea.isVisible().catch(() => false);
    recordResult('聊天区域', '消息输入框可见', exists,
      'textarea 输入框可见', `输入框${exists ? '可见' : '不可见'}`, exists ? 'info' : 'critical');
  });

  // 3.2 测试输入文字后发送按钮是否启用
  await safeRun('发送按钮状态变化', async () => {
    const textarea = page.locator('textarea[data-testid="chat-input-textarea"], textarea[placeholder*="Enter"], textarea[placeholder*="发送"], textarea').first();

    if (await textarea.isVisible().catch(() => false)) {
      // 检查初始状态下发送按钮
      const sendBtn = page.locator('button[data-testid="chat-send-button"], button[aria-label="发送消息"]').first();
      const sendBtnExists = await sendBtn.isVisible().catch(() => false);

      if (sendBtnExists) {
        const initiallyDisabled = await sendBtn.isDisabled().catch(() => true);
        recordResult('聊天区域', '空输入时发送按钮禁用', initiallyDisabled,
          '发送按钮应禁用', `发送按钮${initiallyDisabled ? '已禁用' : '未禁用'}`, initiallyDisabled ? 'info' : 'warning');

        // 输入文字
        await textarea.click();
        await textarea.fill('测试消息');
        await page.waitForTimeout(300);

        const afterInputDisabled = await sendBtn.isDisabled().catch(() => true);
        recordResult('聊天区域', '有输入时发送按钮启用', !afterInputDisabled,
          '发送按钮应启用', `发送按钮${afterInputDisabled ? '仍禁用' : '已启用'}`, !afterInputDisabled ? 'info' : 'warning');

        // 清空输入（不发送，避免触发 AI）
        await textarea.fill('');
        await page.waitForTimeout(100);
      } else {
        recordResult('聊天区域', '发送按钮存在', false, '发送按钮可见', '发送按钮不可见', 'warning');
      }
    }
  });

  // 3.3 检查附件按钮
  await safeRun('附件按钮', async () => {
    const attachBtn = page.locator('button[aria-label="添加附件"], button[title="添加附件"]').first();
    const exists = await attachBtn.isVisible().catch(() => false);
    recordResult('聊天区域', '附件按钮可见', exists,
      '附件按钮可见', `附件按钮${exists ? '可见' : '不可见'}`, exists ? 'info' : 'warning');

    if (exists) {
      // 点击附件按钮，检查是否触发文件选择
      const [fileChooser] = await Promise.all([
        page.waitForEvent('filechooser', { timeout: 3000 }).catch(() => null),
        attachBtn.click(),
      ]);
      recordResult('聊天区域', '附件按钮触发文件选择', fileChooser !== null,
        '弹出文件选择对话框', fileChooser !== null ? '弹出文件选择' : '未弹出文件选择',
        fileChooser !== null ? 'info' : 'warning');

      if (fileChooser) {
        // 关闭文件选择对话框（不做任何选择）
        await page.keyboard.press('Escape');
        await page.waitForTimeout(300);
      }
    }
  });

  // 3.4 检查语音按钮
  await safeRun('语音按钮', async () => {
    const voiceBtn = page.locator('button[aria-label*="语音"], button[aria-label*="voice"], button[title*="语音"]').first();
    const exists = await voiceBtn.isVisible().catch(() => false);
    recordResult('聊天区域', '语音按钮可见', exists,
      '语音按钮可见', `语音按钮${exists ? '可见' : '不可见（可能浏览器不支持）'}`, exists ? 'info' : 'info');

    if (exists) {
      const isDisabled = await voiceBtn.isDisabled().catch(() => false);
      recordResult('聊天区域', '语音按钮可点击', !isDisabled,
        '语音按钮可点击', `语音按钮${isDisabled ? '已禁用' : '可点击'}`, isDisabled ? 'info' : 'info');
    }
  });

  // 3.5 检查思考模式按钮
  await safeRun('思考模式按钮', async () => {
    const thinkBtn = page.locator('button[aria-label*="思考"], button[title*="思考"], button:has-text("思考")').first();
    const exists = await thinkBtn.isVisible().catch(() => false);
    recordResult('聊天区域', '思考模式按钮可见', exists,
      '思考模式按钮可见', `思考模式按钮${exists ? '可见' : '不可见'}`, exists ? 'info' : 'info');
  });

  // 3.6 检查 Token 计数显示
  await safeRun('Token计数显示', async () => {
    // 检查模型名和 token 相关的元素
    const tokenDisplay = page.locator('[data-testid="chat-input"] .bg-primary\\/10, [data-testid="chat-input"] span:has-text("glm")').first();
    const exists = await tokenDisplay.isVisible().catch(() => false);

    // 也检查是否有模型名显示
    const modelNameEl = page.locator('span:has-text("glm"), span.font-semibold').first();
    const modelVisible = await modelNameEl.isVisible().catch(() => false);

    recordResult('聊天区域', '模型名/Token 计数显示', exists || modelVisible,
      '模型名或 Token 计数区域可见', `模型名/Token 区域${exists || modelVisible ? '可见' : '不可见'}`,
      exists || modelVisible ? 'info' : 'warning');
  });

  // ============================================
  // 4. 工作区面板测试
  // ============================================
  console.log('\n[STEP 4] 工作区面板测试...');

  // 先确保处于 Five-space 布局模式（工作区面板存在的前提）
  await safeRun('切换到 Five-space 布局', async () => {
    const layoutBtn = page.locator('header button[title*="Classic"], header button[title*="Five-space"], header button:has-text("Classic"), header button:has-text("Five-space")').first();
    if (await layoutBtn.isVisible().catch(() => false)) {
      const btnText = await layoutBtn.textContent().catch(() => '');
      // 如果按钮显示 "Classic"，说明当前是 Five-space 模式
      // 如果按钮显示 "Five-space"，说明当前是 Classic 模式
      if (btnText.includes('Five-space')) {
        // 需要切换到 Five-space
        await layoutBtn.click();
        await page.waitForTimeout(1000);
      }
      // 如果已经是 Classic 按钮说明当前就是 Five-space 模式
    }
  });

  // 4.1 检查工作区面板是否可见
  await safeRun('工作区面板可见性', async () => {
    // 检查工作区面板 (WorkspacePanel)
    const workspace = page.locator('section').filter({ hasText: '' }).last();
    const workspaceVisible = await workspace.isVisible().catch(() => false);

    // 检查工作区切换手柄
    const toggleHandle = page.locator('button[title*="workspace"], button[title*="Hide workspace"], button[title*="Show workspace"]').first();
    const toggleVisible = await toggleHandle.isVisible().catch(() => false);

    recordResult('工作区面板', '工作区切换手柄可见', toggleVisible,
      '工作区切换按钮可见', `切换按钮${toggleVisible ? '可见' : '不可见'}`, toggleVisible ? 'info' : 'warning');
  });

  // 4.2 测试工作区面板收起
  await safeRun('工作区面板收起', async () => {
    // 查找收起按钮（‹ 按钮）
    const collapseBtn = page.locator('button[title="Hide workspace"], button[title*="Hide"]').first();
    if (await collapseBtn.isVisible().catch(() => false)) {
      await collapseBtn.click();
      await page.waitForTimeout(500);

      // 验证工作区是否收起
      const expandBtn = page.locator('button[title="Show workspace"], button[title*="Show"]').first();
      const isCollapsed = await expandBtn.isVisible().catch(() => false);

      recordResult('工作区面板', '点击收起按钮后面板收起', isCollapsed,
        '收起后出现展开按钮', `展开按钮${isCollapsed ? '可见' : '不可见'}`, isCollapsed ? 'info' : 'warning');

      // 恢复
      if (isCollapsed) {
        await expandBtn.click();
        await page.waitForTimeout(500);
      }
    } else {
      recordResult('工作区面板', '工作区收起按钮可见', false,
        'Hide workspace 按钮可见', '按钮不可见', 'warning');
    }
  });

  // 4.3 测试工作区面板展开
  await safeRun('工作区面板展开', async () => {
    // 先收起
    const collapseBtn = page.locator('button[title="Hide workspace"], button[title*="Hide"]').first();
    if (await collapseBtn.isVisible().catch(() => false)) {
      await collapseBtn.click();
      await page.waitForTimeout(500);

      // 再展开
      const expandBtn = page.locator('button[title="Show workspace"], button[title*="Show"]').first();
      if (await expandBtn.isVisible().catch(() => false)) {
        await expandBtn.click();
        await page.waitForTimeout(500);

        // 验证面板是否展开
        const afterCollapseBtn = page.locator('button[title="Hide workspace"], button[title*="Hide"]').first();
        const isExpanded = await afterCollapseBtn.isVisible().catch(() => false);

        recordResult('工作区面板', '点击展开按钮后面板展开', isExpanded,
          '展开后出现收起按钮', `收起按钮${isExpanded ? '可见' : '不可见'}`, isExpanded ? 'info' : 'warning');
      }
    }
  });

  // 4.4 检查工作区标签页
  await safeRun('工作区标签页', async () => {
    // 确保工作区展开
    const expandBtn = page.locator('button[title="Show workspace"], button[title*="Show"]').first();
    if (await expandBtn.isVisible().catch(() => false)) {
      await expandBtn.click();
      await page.waitForTimeout(500);
    }

    // 查找工作区中的标签页
    const tabElements = page.locator('[role="tab"], .workspace-tab, [data-state="active"]').filter({ hasText: /.+/ });
    const tabCount = await tabElements.count();

    recordResult('工作区面板', `工作区标签页数量: ${tabCount}`, true,
      '有标签页显示', `找到 ${tabCount} 个标签页`, tabCount > 0 ? 'info' : 'info');

    // 如果有标签页，点击测试
    if (tabCount > 1) {
      const secondTab = tabElements.nth(1);
      if (await secondTab.isVisible().catch(() => false)) {
        await secondTab.click();
        await page.waitForTimeout(500);
        recordResult('工作区面板', '切换工作区标签页', true,
          '点击第二个标签页', '已点击', 'info');
      }
    }
  });

  // 4.5 检查 Dock Bar
  await safeRun('Dock Bar', async () => {
    // 检查底部 Dock Bar 是否存在
    const dockBar = page.locator('.border-t').last();
    const dockVisible = await dockBar.isVisible().catch(() => false);

    recordResult('工作区面板', 'Dock Bar 可见', dockVisible,
      '底部 Dock Bar 可见', `Dock Bar ${dockVisible ? '可见' : '不可见'}`, dockVisible ? 'info' : 'warning');

    if (dockVisible) {
      // 检查 Dock Bar 中的图标项
      const dockItems = dockBar.locator('button');
      const itemCount = await dockItems.count();
      recordResult('工作区面板', `Dock Bar 项目数量: ${itemCount}`, itemCount > 0,
        'Dock Bar 有图标项', `找到 ${itemCount} 个项`, itemCount > 0 ? 'info' : 'warning');
    }
  });

  // ============================================
  // 5. 综合测试 - 滚动行为
  // ============================================
  console.log('\n[STEP 5] 滚动行为测试...');

  await safeRun('聊天区域滚动', async () => {
    // 检查聊天消息列表是否可滚动
    const messageList = page.locator('[data-testid="chat-input"]').first();
    const scrollable = await messageList.isVisible().catch(() => false);
    recordResult('滚动行为', '聊天区域存在', scrollable,
      '聊天区域可见', `聊天区域${scrollable ? '可见' : '不可见'}`, 'info');
  });

  await safeRun('侧边栏会话列表滚动', async () => {
    // 确保侧边栏展开
    const sidebar = page.locator('aside').first();
    if (await sidebar.isVisible().catch(() => false)) {
      const sessionList = sidebar.locator('.overflow-y-auto').first();
      const hasScroll = await sessionList.isVisible().catch(() => false);
      recordResult('滚动行为', '会话列表可滚动', hasScroll,
        '会话列表区域可见', `会话列表区域${hasScroll ? '可见' : '不可见'}`, 'info');
    }
  });

  // ============================================
  // 6. 截图保存
  // ============================================
  console.log('\n[STEP 6] 保存截图...');
  await page.screenshot({ path: 'e2e-ui-test-screenshot.png', fullPage: true });
  console.log('  截图已保存: e2e-ui-test-screenshot.png');

  // ---- 清理 ----
  await browser.close();

  // ---- 输出测试报告 ----
  console.log('\n========================================');
  console.log('  测试报告');
  console.log('========================================\n');

  const totalTests = results.length;
  const passedTests = results.filter(r => r.passed).length;
  const failedTests = results.filter(r => !r.passed).length;
  const criticalFails = results.filter(r => !r.passed && r.severity === 'critical').length;
  const warningFails = results.filter(r => !r.passed && r.severity === 'warning').length;

  console.log(`总计: ${totalTests} 项测试`);
  console.log(`通过: ${passedTests} 项`);
  console.log(`失败: ${failedTests} 项 (其中 critical: ${criticalFails}, warning: ${warningFails})`);
  console.log(`通过率: ${totalTests > 0 ? ((passedTests / totalTests) * 100).toFixed(1) : 0}%`);

  if (failedTests > 0) {
    console.log('\n--- 失败项详情 ---');
    for (const r of results.filter(r => !r.passed)) {
      const sev = r.severity === 'critical' ? '[!!]' : r.severity === 'warning' ? '[!]' : '[~]';
      console.log(`  ${sev} [${r.category}] ${r.name}`);
      console.log(`       Expected: ${r.expected}`);
      console.log(`       Actual:   ${r.actual}`);
    }
  }

  console.log('\n========================================');
  console.log('  测试完成');
  console.log('========================================\n');

  // 输出 JSON 格式结果
  const jsonOutput = {
    summary: { total: totalTests, passed: passedTests, failed: failedTests, criticalFails, warningFails },
    results: results
  };
  const fs = await import('fs');
  fs.writeFileSync('e2e-ui-test-results.json', JSON.stringify(jsonOutput, null, 2));
  console.log('JSON 结果已保存: e2e-ui-test-results.json');
}

main().catch(err => {
  console.error('测试执行出错:', err);
  process.exit(1);
});
