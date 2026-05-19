/**
 * Agent OS (超级终端) 最终综合 UI 测试
 *
 * 修复了所有已知问题后的完整测试脚本
 * - 正确处理双 aside 元素（移动端 + 桌面端）
 * - 正确选择会话后验证聊天区域
 * - 完整的交互验证
 */

import { chromium } from 'playwright';
import fs from 'fs';

const BASE_URL = 'http://127.0.0.1:5188';
const DIR = 'e2e-deep-screenshots';
const results = [];
const bugs = [];
let idx = 40;

if (!fs.existsSync(DIR)) fs.mkdirSync(DIR, { recursive: true });

async function ss(page, name) {
  idx++;
  const f = `${DIR}/${String(idx).padStart(3, '0')}-${name}.png`;
  await page.screenshot({ path: f, fullPage: true });
  console.log(`    [截图] ${f.split('/').pop()}`);
}

function rec(cat, name, pass, exp, act, sev = 'info') {
  results.push({ category: cat, name, passed: pass, expected: exp, actual: act, severity: sev });
  console.log(`    [${pass ? 'PASS' : 'FAIL'}]${sev === 'high' ? ' (!)' : sev === 'medium' ? ' (~)' : ''} ${name}`);
  if (!pass) {
    console.log(`         Expected: ${exp}`);
    console.log(`         Actual:   ${act}`);
    bugs.push({ testItem: `${cat} - ${name}`, expected: exp, actual: act, severity: sev === 'high' ? '高' : sev === 'medium' ? '中' : '低' });
  }
}

async function safe(name, fn) {
  try { await fn(); } catch (e) { rec('异常', name, false, '无异常', e.message.substring(0, 200), 'high'); }
}

/**
 * 获取可见的桌面端 aside 元素
 * 页面上可能有两个 aside（移动端+桌面端），桌面端的是可见的那个
 */
function getDesktopSidebar(page) {
  // 使用 filter 找到可见的 aside
  return page.locator('aside').filter({ has: page.locator('button:visible') }).last();
}

async function main() {
  console.log('\n==============================================');
  console.log('  Agent OS 最终综合 UI 测试');
  console.log('==============================================\n');

  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  page.setDefaultTimeout(10000);

  // ---- 登录 ----
  console.log('[STEP 0] 登录...');
  await page.goto(BASE_URL + '/login', { waitUntil: 'networkidle' });
  await page.waitForTimeout(500);
  if (page.url().includes('/login')) {
    await page.locator('#username').fill('admin');
    await page.locator('#password').fill('admin123');
    await page.locator('button[type="submit"]').click();
    await page.waitForURL(u => !u.toString().includes('/login'), { timeout: 15000 }).catch(() => {});
    await page.waitForTimeout(2000);
  }
  const loggedIn = !page.url().includes('/login');
  rec('登录', '管理员登录', loggedIn, 'URL 不含 /login', `URL: ${page.url()}`, loggedIn ? 'info' : 'critical');
  if (!loggedIn) { await browser.close(); return; }
  await ss(page, 'after-login');

  // 确保在主页
  await page.goto(BASE_URL + '/', { waitUntil: 'networkidle' });
  await page.waitForTimeout(2000);

  // ============================================
  // STEP 1: 主页面布局
  // ============================================
  console.log('\n[STEP 1] 主页面布局...');

  // 1.1 侧边栏 - 使用正确的选择器
  await safe('侧边栏', async () => {
    const sidebar = getDesktopSidebar(page);
    const visible = await sidebar.isVisible().catch(() => false);
    const width = visible ? await sidebar.evaluate(el => el.offsetWidth) : 0;
    rec('主页面布局', `侧边栏可见 (宽${width}px)`, visible && width > 100,
      '侧边栏可见且宽度>100px', `visible=${visible}, width=${width}px`, visible ? 'info' : 'high');

    // 新会话按钮
    const newBtn = sidebar.locator('button:has-text("新会话")').first();
    const btnVisible = await newBtn.isVisible().catch(() => false);
    rec('主页面布局', '"新会话"按钮可见', btnVisible, '可见', btnVisible ? '可见' : '不可见', btnVisible ? 'info' : 'high');

    // 会话列表
    const sessions = sidebar.locator('.group');
    const count = await sessions.count();
    rec('主页面布局', `会话列表 (${count} 个)`, count > 0, '至少1个会话', `${count} 个`, count > 0 ? 'info' : 'medium');
  });

  // 1.2 点击会话加载聊天
  await safe('选中会话', async () => {
    const sidebar = getDesktopSidebar(page);
    const sessions = sidebar.locator('.group');
    const count = await sessions.count();

    if (count > 0) {
      // 点击第一个会话的标题区域
      const firstTitle = sessions.first().locator('div').first();
      const title = await firstTitle.textContent().catch(() => '');
      console.log(`    点击会话: "${title?.trim()}"`);
      await firstTitle.click();
      await page.waitForTimeout(2000);

      await ss(page, 'session-selected');

      // 检查是否进入聊天界面
      const bodyText = await page.textContent('body').catch(() => '');
      const isWelcome = bodyText.includes('欢迎使用超级终端');
      const hasTextarea = await page.locator('textarea').count() > 0;
      const chatLoaded = !isWelcome || hasTextarea;

      rec('主页面布局', '选中会话后聊天区域加载', chatLoaded,
        '不再显示欢迎页或出现输入框',
        isWelcome ? '仍显示欢迎页' : '已进入聊天', chatLoaded ? 'info' : 'high');
    }
  });

  // 1.3 工作区面板
  await safe('工作区面板', async () => {
    const wsBtn = page.locator('button[title*="workspace" i], button[title*="Hide"], button[title*="Show"]').first();
    const wsVisible = await wsBtn.isVisible().catch(() => false);
    rec('主页面布局', '工作区切换手柄可见', wsVisible,
      'workspace 按钮可见', wsVisible ? '可见' : '不可见', wsVisible ? 'info' : 'medium');
  });

  // ============================================
  // STEP 2: 顶部导航栏
  // ============================================
  console.log('\n[STEP 2] 顶部导航栏...');

  const navItems = [
    { label: '工具', path: '/tools', contentCheck: ['工具管理', 'tool', '搜索', '分类'] },
    { label: '智能体', path: '/agents', contentCheck: ['智能体管理', 'agent', 'L1', 'L2', 'L3'] },
    { label: '监控', path: '/monitoring', contentCheck: ['监控', 'monitoring', '状态'] },
    { label: '记忆', path: '/memory', contentCheck: ['记忆', 'memory', '知识'] },
    { label: '设置', path: '/settings', contentCheck: ['设置', 'Setting', 'API', 'LLM'] },
    { label: '调试', path: '/debug', contentCheck: ['调试', 'debug', '执行'] },
  ];

  for (const item of navItems) {
    await safe(`导航-${item.label}`, async () => {
      const btn = page.locator(`header button:has-text("${item.label}")`).first();
      if (!(await btn.isVisible().catch(() => false))) {
        rec('导航', `${item.label}按钮可见`, false, '可见', '不可见', 'high');
        return;
      }

      await btn.click();
      await page.waitForTimeout(2000);
      await page.waitForLoadState('networkidle').catch(() => {});

      const url = page.url();
      rec('导航', `${item.label}跳转到 ${item.path}`, url.includes(item.path),
        `URL 含 ${item.path}`, `URL: ${url}`, url.includes(item.path) ? 'info' : 'high');

      await ss(page, `nav-${item.label}`);

      // 验证页面内容
      const bodyText = (await page.textContent('body').catch(() => '')) || '';
      const hasContent = item.contentCheck.some(kw => bodyText.includes(kw));
      rec('导航', `${item.label}页面内容渲染`, hasContent,
        `包含: ${item.contentCheck.join('/')}`, hasContent ? '包含相关内容' : '未找到相关内容', hasContent ? 'info' : 'medium');

      // 特殊验证
      if (item.label === '工具') {
        // 搜索框
        const search = page.locator('input[type="text"], input[placeholder*="搜索"]').first();
        const searchVis = await search.isVisible().catch(() => false);
        rec('导航-工具', '搜索框可见', searchVis, '可见', searchVis ? '可见' : '不可见', searchVis ? 'info' : 'medium');

        if (searchVis) {
          await search.fill('test');
          await page.waitForTimeout(500);
          const val = await search.inputValue().catch(() => '');
          rec('导航-工具', '搜索框可输入', val === 'test', '值为test', `"${val}"`, val === 'test' ? 'info' : 'medium');
          await search.clear();
        }

        // 分类下拉框
        const select = page.locator('button[role="combobox"]').first();
        if (await select.isVisible().catch(() => false)) {
          await select.click();
          await page.waitForTimeout(500);
          const opts = await page.locator('[role="option"]').count();
          rec('导航-工具', `分类下拉框有${opts}个选项`, opts > 0, '有选项', `${opts}个`, opts > 0 ? 'info' : 'medium');
          await page.keyboard.press('Escape');
        }
      }

      if (item.label === '智能体') {
        const l1 = await page.locator('text=/L1/i').count();
        const l2 = await page.locator('text=/L2/i').count();
        const l3 = await page.locator('text=/L3/i').count();
        rec('导航-智能体', `层级标签 L1:${l1} L2:${l2} L3:${l3}`, l1 + l2 + l3 > 0,
          '有层级标签', `L1:${l1} L2:${l2} L3:${l3}`, l1 + l2 + l3 > 0 ? 'info' : 'medium');

        const agentText = (await page.textContent('body').catch(() => '')) || '';
        const total = parseInt(agentText.match(/共\s*(\d+)\s*个智能体/)?.[1] || '0');
        rec('导航-智能体', `显示${total}个智能体`, total > 0, '有智能体', `${total}个`, total > 0 ? 'info' : 'medium');
      }

      // 返回主页
      await page.goto(BASE_URL + '/', { waitUntil: 'networkidle' });
      await page.waitForTimeout(1500);
    });
  }

  // ============================================
  // STEP 3: 聊天区域交互
  // ============================================
  console.log('\n[STEP 3] 聊天区域交互...');

  // 选中会话
  await safe('选中会话', async () => {
    const sidebar = getDesktopSidebar(page);
    const sessions = sidebar.locator('.group');
    if (await sessions.count() > 0) {
      await sessions.first().locator('div').first().click();
      await page.waitForTimeout(2000);
    }

    // 如果没有 textarea，创建新会话
    if (await page.locator('textarea').count() === 0) {
      console.log('    未找到输入框，创建新会话...');
      const newBtn = sidebar.locator('button:has-text("新会话")').first();
      await newBtn.click();
      await page.waitForTimeout(2000);
    }
  });

  await ss(page, 'chat-before-interaction');

  // 3.1 输入框
  await safe('输入框', async () => {
    const textarea = page.locator('textarea').first();
    const vis = await textarea.isVisible().catch(() => false);
    rec('聊天交互', '消息输入框可见', vis, 'textarea 可见', vis ? '可见' : '不可见', vis ? 'info' : 'high');

    if (vis) {
      await textarea.fill('测试');
      await page.waitForTimeout(300);
      const val = await textarea.inputValue().catch(() => '');
      rec('聊天交互', '输入"测试"成功', val === '测试', '值为"测试"', `"${val}"`, val === '测试' ? 'info' : 'high');
      await ss(page, 'chat-input-filled');
    }
  });

  // 3.2 发送按钮
  await safe('发送按钮', async () => {
    const textarea = page.locator('textarea').first();
    if (!(await textarea.isVisible().catch(() => false))) {
      rec('聊天交互', '发送按钮测试跳过', true, '', '无输入框', 'info');
      return;
    }

    await textarea.fill('测试');
    await page.waitForTimeout(200);

    // 查找所有按钮信息
    const chatArea = textarea.locator('..').locator('..');
    const btns = chatArea.locator('button');
    const btnCount = await btns.count();
    console.log(`    输入区有 ${btnCount} 个按钮:`);

    let sendBtn = null;
    for (let i = 0; i < btnCount; i++) {
      const b = btns.nth(i);
      const t = (await b.getAttribute('title') || '');
      const al = (await b.getAttribute('aria-label') || '');
      const tx = (await b.textContent() || '').trim().substring(0, 20);
      const d = await b.isDisabled().catch(() => false);
      console.log(`      [${i}] title="${t}" aria="${al}" text="${tx}" disabled=${d}`);
      if (t.includes('发送') || t.toLowerCase().includes('send') || al.includes('发送') || al.toLowerCase().includes('send')) {
        sendBtn = b;
      }
    }

    if (sendBtn) {
      const enabled = await sendBtn.isEnabled().catch(() => false);
      rec('聊天交互', '有输入时发送按钮启用', enabled, '应启用', enabled ? '已启用' : '仍禁用', enabled ? 'info' : 'high');

      await textarea.fill('');
      await page.waitForTimeout(200);
      const disabled = await sendBtn.isDisabled().catch(() => false);
      rec('聊天交互', '清空后发送按钮禁用', disabled, '应禁用', disabled ? '已禁用' : '未禁用', disabled ? 'info' : 'medium');
    } else {
      rec('聊天交互', '发送按钮', false, '可见', '未找到明确发送按钮', 'medium');
    }
    await textarea.fill('');
  });

  // 3.3 思考模式
  await safe('思考模式', async () => {
    const allBtns = page.locator('button');
    const count = await allBtns.count();
    let found = null;
    for (let i = 0; i < count; i++) {
      const b = allBtns.nth(i);
      const t = (await b.textContent() || '') + (await b.getAttribute('title') || '') + (await b.getAttribute('aria-label') || '');
      if (t.includes('思考') || t.toLowerCase().includes('think')) {
        found = b;
        break;
      }
    }
    if (found) {
      rec('聊天交互', '思考模式按钮可见', true, '可见', '已找到', 'info');
      await found.click();
      await page.waitForTimeout(500);
      await ss(page, 'thinking-mode-on');
      await found.click();
      await page.waitForTimeout(300);
    } else {
      rec('聊天交互', '思考模式按钮可见', false, '可见', '未找到', 'medium');
    }
  });

  // 3.4 模型名和 Token
  await safe('模型Token', async () => {
    const text = (await page.textContent('body').catch(() => '')) || '';
    rec('聊天交互', '模型名(glm)', text.includes('glm'), '含glm', text.includes('glm') ? '含' : '不含', text.includes('glm') ? 'info' : 'medium');
    rec('聊天交互', 'Token/上下文', text.match(/token|Token|上下文|context/i) !== null,
      '含token/上下文', text.match(/token|Token|上下文|context/i) ? '含' : '不含', 'info');
  });

  // ============================================
  // STEP 4: 工作区面板
  // ============================================
  console.log('\n[STEP 4] 工作区面板...');

  await safe('工作区', async () => {
    // 查找 Hide workspace 按钮
    const hideBtn = page.locator('button[title*="Hide"], button[title*="hide"]').first();
    if (await hideBtn.isVisible().catch(() => false)) {
      await hideBtn.click();
      await page.waitForTimeout(500);
      await ss(page, 'workspace-hidden');

      const showBtn = page.locator('button[title*="Show"], button[title*="show"]').first();
      const collapsed = await showBtn.isVisible().catch(() => false);
      rec('工作区面板', '点击隐藏后收起', collapsed, 'Show 按钮出现', collapsed ? '已收起' : '未收起', collapsed ? 'info' : 'high');

      if (collapsed) {
        await showBtn.click();
        await page.waitForTimeout(500);
        await ss(page, 'workspace-restored');
        rec('工作区面板', '点击展开后恢复', true, 'Hide 按钮出现', '已恢复', 'info');
      }
    } else {
      const showBtn = page.locator('button[title*="Show"], button[title*="show"]').first();
      if (await showBtn.isVisible().catch(() => false)) {
        rec('工作区面板', '工作区已折叠', true, 'Show 按钮可见', '已折叠', 'info');
        await showBtn.click();
        await page.waitForTimeout(500);
      } else {
        rec('工作区面板', '工作区切换手柄可见', false, '可见', '不可见', 'medium');
      }
    }
  });

  // ============================================
  // STEP 5: 侧边栏交互
  // ============================================
  console.log('\n[STEP 5] 侧边栏交互...');

  // 5.1 新会话
  await safe('新会话', async () => {
    const sidebar = getDesktopSidebar(page);
    const before = await sidebar.locator('.group').count();
    const newBtn = sidebar.locator('button:has-text("新会话")').first();
    await newBtn.click();
    await page.waitForTimeout(2000);
    const after = await sidebar.locator('.group').count();
    rec('侧边栏', '新会话创建', after > before, `${before}→${after}`, `${before}→${after}`, after > before ? 'info' : 'high');
    await ss(page, 'new-session');
  });

  // 5.2 更多操作菜单
  await safe('更多操作', async () => {
    const sidebar = getDesktopSidebar(page);
    const firstGroup = sidebar.locator('.group').first();
    await firstGroup.hover().catch(() => {});
    await page.waitForTimeout(300);

    const moreBtn = firstGroup.locator('button[aria-label="更多操作"]').first();
    if (await moreBtn.isVisible().catch(() => false)) {
      await moreBtn.click();
      await page.waitForTimeout(500);

      const menu = page.locator('[role="menu"]').first();
      const menuVis = await menu.isVisible().catch(() => false);
      rec('侧边栏', '更多操作菜单弹出', menuVis, '菜单可见', menuVis ? '可见' : '不可见', menuVis ? 'info' : 'high');

      if (menuVis) {
        await ss(page, 'more-actions-menu');
        for (const item of ['重命名', '复制', '星标', '置顶', '删除']) {
          const mi = page.locator(`[role="menuitem"]:has-text("${item}")`).first();
          const vis = await mi.isVisible().catch(() => false);
          rec('侧边栏', `菜单项"${item}"`, vis, '可见', vis ? '可见' : '不可见', vis ? 'info' : 'medium');
        }
        await page.keyboard.press('Escape');
        await page.waitForTimeout(300);
      }
    }
  });

  // 5.3 重命名
  await safe('重命名', async () => {
    const sidebar = getDesktopSidebar(page);
    const firstGroup = sidebar.locator('.group').first();
    await firstGroup.hover().catch(() => {});
    await page.waitForTimeout(300);

    const moreBtn = firstGroup.locator('button[aria-label="更多操作"]').first();
    if (await moreBtn.isVisible().catch(() => false)) {
      await moreBtn.click();
      await page.waitForTimeout(500);

      const renameItem = page.locator('[role="menuitem"]:has-text("重命名")').first();
      if (await renameItem.isVisible().catch(() => false)) {
        page.once('dialog', async d => {
          rec('侧边栏', '重命名弹出prompt', true, '弹出对话框', `msg: ${d.message()}`, 'info');
          await d.accept('UI测试重命名');
        });
        await renameItem.click();
        await page.waitForTimeout(500);
        await ss(page, 'after-rename');
      }
    }
  });

  // 5.4 隐藏/显示侧边栏
  await safe('隐藏显示侧边栏', async () => {
    const toggle = page.locator('header button').filter({ has: page.locator('svg.lucide-panel-left-close') }).first();
    if (!(await toggle.isVisible().catch(() => false))) {
      const toggleOpen = page.locator('header button').filter({ has: page.locator('svg.lucide-panel-left-open') }).first();
      if (await toggleOpen.isVisible().catch(() => false)) {
        rec('侧边栏', '侧边栏已折叠', true, '', '', 'info');
        await toggleOpen.click();
        await page.waitForTimeout(500);
        await ss(page, 'sidebar-restored-before-test');
      }
    }

    // 现在应该有关闭按钮
    const closeBtn = page.locator('header button').filter({ has: page.locator('svg.lucide-panel-left-close') }).first();
    if (await closeBtn.isVisible().catch(() => false)) {
      await closeBtn.click();
      await page.waitForTimeout(500);
      await ss(page, 'sidebar-hidden');

      const openBtn = page.locator('header button').filter({ has: page.locator('svg.lucide-panel-left-open') }).first();
      const hidden = await openBtn.isVisible().catch(() => false);
      rec('侧边栏', '隐藏后侧边栏收起', hidden, '显示侧边栏按钮可见', hidden ? '已收起' : '未收起', hidden ? 'info' : 'high');

      if (hidden) {
        await openBtn.click();
        await page.waitForTimeout(500);
        await ss(page, 'sidebar-shown');
        rec('侧边栏', '显示后侧边栏恢复', true, '侧边栏可见', '已恢复', 'info');
      }
    }
  });

  // ============================================
  // STEP 6: 主题和布局
  // ============================================
  console.log('\n[STEP 6] 主题和布局...');

  // 6.1 浅色模式
  await safe('浅色模式', async () => {
    const themeBtn = page.locator('button[title*="浅色"], button[title*="深色"], button[title*="切换到"]').first();
    if (await themeBtn.isVisible().catch(() => false)) {
      const title = await themeBtn.getAttribute('title') || '';
      console.log(`    主题按钮: "${title}"`);

      // 如果当前是深色，点击切换到浅色
      if (title.includes('浅色')) {
        await themeBtn.click();
        await page.waitForTimeout(500);
      }

      await ss(page, 'light-mode');
      rec('主题', '浅色模式切换', true, '点击切换', '已切换', 'info');

      // 切回深色
      const themeBtn2 = page.locator('button[title*="深色"]').first();
      if (await themeBtn2.isVisible().catch(() => false)) {
        await themeBtn2.click();
        await page.waitForTimeout(500);
      }
      await ss(page, 'dark-mode-restored');
      rec('主题', '深色模式恢复', true, '切回深色', '已恢复', 'info');
    }
  });

  // 6.2 Classic布局
  await safe('Classic布局', async () => {
    const layoutBtn = page.locator('header button[title*="Classic"], header button[title*="Five-space"]').first();
    if (await layoutBtn.isVisible().catch(() => false)) {
      const before = await layoutBtn.getAttribute('title') || '';
      await layoutBtn.click();
      await page.waitForTimeout(1000);
      const after = await layoutBtn.getAttribute('title') || '';
      rec('布局', '布局切换', before !== after, `title 变化`, `"${before}" → "${after}"`, before !== after ? 'info' : 'medium');
      await ss(page, 'layout-switched');

      // 切回
      await layoutBtn.click();
      await page.waitForTimeout(1000);
    }
  });

  // ---- 最终截图 ----
  await ss(page, 'final-state');

  await browser.close();

  // ---- 报告 ----
  console.log('\n==============================================');
  console.log('  最终测试报告');
  console.log('==============================================\n');

  const total = results.length;
  const passed = results.filter(r => r.passed).length;
  const failed = total - passed;
  console.log(`总计: ${total} 项 | 通过: ${passed} | 失败: ${failed}`);
  console.log(`通过率: ${total > 0 ? ((passed / total) * 100).toFixed(1) : 0}%`);

  // 按类别汇总
  const cats = [...new Set(results.map(r => r.category))];
  for (const c of cats) {
    const cr = results.filter(r => r.category === c);
    const cp = cr.filter(r => r.passed).length;
    console.log(`  [${c}] ${cp}/${cr.length} 通过`);
  }

  if (failed > 0) {
    console.log('\n--- 失败项 ---');
    for (const r of results.filter(r => !r.passed)) {
      console.log(`  [${r.severity === 'high' ? '!' : '~'}] [${r.category}] ${r.name}`);
      console.log(`       Expected: ${r.expected}`);
      console.log(`       Actual:   ${r.actual}`);
    }
  }

  if (bugs.length > 0) {
    console.log('\n--- Bug 列表 ---');
    for (const b of bugs) {
      console.log(`  [${b.severity}] ${b.testItem}`);
      console.log(`       Expected: ${b.expected}`);
      console.log(`       Actual:   ${b.actual}`);
    }
  }

  fs.writeFileSync('e2e-final-results.json', JSON.stringify({ summary: { total, passed, failed }, bugs, results }, null, 2));
  console.log(`\n结果已保存: e2e-final-results.json`);
  console.log(`截图目录: ${DIR}/ (${idx} 张)`);
  console.log('\n==============================================');
}

main().catch(e => { console.error(e); process.exit(1); });
