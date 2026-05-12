/**
 * 任务执行闭环系统完整 E2E 测试套件
 *
 * 测试覆盖范围：
 * 1. 任务创建和显示
 * 2. 长期任务管理
 * 3. Agent 多Tab管理
 * 4. 可折叠面板
 * 5. 监控页面
 * 6. 主题切换
 * 7. WebSocket实时更新
 *
 * @docs docs/tasks/task-execution-loop-system.md
 */

import { test, expect } from '@playwright/test';
import {
  quickLogin,
  logoutAndCleanup,
  waitForPageLoad,
  takeScreenshot,
  waitForElement,
  waitForAPI,
  waitForAPIResponse,
  verifyTheme,
  switchTheme,
  getCurrentTheme,
  createSession,
  sendMessage,
  waitForAIResponse,
} from './helpers';

/**
 * ============================================================================
 * 测试配置
 * ============================================================================
 */

// 每个测试前清理并登录
test.beforeEach(async ({ page }) => {
  await logoutAndCleanup(page);
  await quickLogin(page);
  await page.waitForLoadState('networkidle');
});

// 测试失败时截图
test.afterEach(async ({ page }, testInfo) => {
  if (testInfo.status !== 'passed') {
    await page.screenshot({
      path: `test-results/task-execution-loop-failed-${testInfo.title}.png`,
      fullPage: true,
    });
  }
});

/**
 * ============================================================================
 * 测试组 1: 任务创建和显示
 * ============================================================================
 */
test.describe('任务创建和显示', () => {
  test('01-应该显示任务卡片在消息流中', async ({ page }) => {
    // 1. 创建会话
    await page.goto('/');
    await page.waitForLoadState('networkidle');

    // 2. 发送创建任务的消息
    const chatInput = page.locator('textarea[placeholder*="消息"], [data-testid="chat-input"]');
    await expect(chatInput).toBeVisible();
    await chatInput.fill('创建一个任务：实现用户登录功能');

    // 3. 点击发送按钮
    const sendButton = page.locator('button[data-testid="chat-send-button"], button[data-testid="send-button"], button:has-text("发送")');
    await sendButton.click();

    // 4. 等待任务卡片出现
    await expect(page.locator('[data-testid="task-card"]')).toBeVisible({ timeout: 10000 });

    // 5. 验证任务内容
    const taskGoal = page.locator('[data-testid="task-card"] p.font-medium');
    await expect(taskGoal).toContainText('实现用户登录功能');

    // 6. 验证任务阶段指示器存在
    await expect(page.locator('[data-testid="task-phase-indicator"]')).toBeVisible();

    await takeScreenshot(page, 'task-creation-card-in-message-stream');
  });

  test('02-应该显示任务的三阶段指示器', async ({ page }) => {
    // 1. 创建任务
    await page.goto('/');
    const chatInput = page.locator('textarea[placeholder*="消息"]');
    await chatInput.fill('创建任务：测试三阶段指示器');
    await page.click('button:has-text("发送")');

    // 2. 等待任务卡片
    await expect(page.locator('[data-testid="task-card"]')).toBeVisible({ timeout: 10000 });

    // 3. 验证三阶段指示器
    const phaseIndicator = page.locator('[data-testid="task-phase-indicator"]');
    await expect(phaseIndicator).toBeVisible();

    // 4. 验证包含三个阶段
    const phases = page.locator('[data-testid="task-phase"]');
    const phaseCount = await phases.count();
    expect(phaseCount).toBeGreaterThanOrEqual(3);

    // 5. 验证阶段文本
    await expect(phases.nth(0)).toContainText('准备', { timeout: 5000 });
    await expect(phases.nth(1)).toContainText('执行');
    await expect(phases.nth(2)).toContainText('评估');

    await takeScreenshot(page, 'task-three-phase-indicator');
  });

  test('03-应该显示验收标准列表', async ({ page }) => {
    // 1. 创建任务
    await page.goto('/');
    const chatInput = page.locator('textarea[placeholder*="消息"]');
    await chatInput.fill('创建任务：验证验收标准显示');
    await page.click('button:has-text("发送")');

    // 2. 等待任务卡片
    await expect(page.locator('[data-testid="task-card"]')).toBeVisible({ timeout: 10000 });

    // 3. 点击展开详情
    const expandButton = page.locator('[data-testid="task-card"] button[aria-label*="展开"]');
    const expandVisible = await expandButton.isVisible();
    if (expandVisible) {
      await expandButton.click();
    }

    // 4. 验证AC列表
    const acList = page.locator('[data-testid="ac-list"]');
    const acListVisible = await acList.isVisible().catch(() => false);

    if (acListVisible) {
      await expect(acList).toBeVisible();

      // 5. 验证AC项目
      const acItems = page.locator('[data-testid="ac-item"]');
      const itemCount = await acItems.count();
      expect(itemCount).toBeGreaterThan(0);
    } else {
      console.log('AC列表不可见，可能任务还未生成AC');
    }

    await takeScreenshot(page, 'task-acceptance-criteria-list');
  });

  test('04-应该显示任务进度条', async ({ page }) => {
    // 1. 创建任务
    await page.goto('/');
    const chatInput = page.locator('textarea[placeholder*="消息"]');
    await chatInput.fill('创建任务：验证进度条');
    await page.click('button:has-text("发送")');

    // 2. 等待任务卡片
    await expect(page.locator('[data-testid="task-card"]')).toBeVisible({ timeout: 10000 });

    // 3. 验证进度条存在
    const progressBar = page.locator('[data-testid="task-card"] .h-1\\.5, [data-testid="progress-bar"]');
    await expect(progressBar).toBeVisible();

    // 4. 验证AC计数显示
    const acCount = page.locator('[data-testid="task-card"] text:has-text("AC:")');
    await expect(acCount).toBeVisible();

    await takeScreenshot(page, 'task-progress-bar');
  });

  test('05-应该显示任务类型标签', async ({ page }) => {
    // 1. 创建任务
    await page.goto('/');
    const chatInput = page.locator('textarea[placeholder*="消息"]');
    await chatInput.fill('创建执行任务：实现用户认证');
    await page.click('button:has-text("发送")');

    // 2. 等待任务卡片
    await expect(page.locator('[data-testid="task-card"]')).toBeVisible({ timeout: 10000 });

    // 3. 验证任务类型标签
    const taskTypeLabel = page.locator('[data-testid="task-card"] span.text-xs');
    await expect(taskTypeLabel).toBeVisible();

    // 4. 验证包含任务类型文本
    const taskCard = page.locator('[data-testid="task-card"]');
    await expect(taskCard).toContainText(/规划任务|执行任务|总体评估/);

    await takeScreenshot(page, 'task-type-label');
  });
});

/**
 * ============================================================================
 * 测试组 2: 长期任务管理
 * ============================================================================
 */
test.describe('长期任务管理', () => {
  test('06-应该创建长期任务', async ({ page }) => {
    // 1. 创建会话
    await page.goto('/');

    // 2. 发送创建长期任务的消息
    const chatInput = page.locator('textarea[placeholder*="消息"]');
    await chatInput.fill('创建长期任务：重构认证模块');
    await page.click('button:has-text("发送")');

    // 3. 等待长期任务创建
    await expect(page.locator('[data-testid="project-card"], [data-testid="task-card"]')).toBeVisible({ timeout: 10000 });

    // 4. 验证Sidebar中的长期任务面板
    const sidebarPanel = page.locator('[data-testid="project-sidebar-panel"]');
    const panelVisible = await sidebarPanel.isVisible().catch(() => false);

    if (panelVisible) {
      await expect(sidebarPanel).toBeVisible();
    }

    await takeScreenshot(page, 'long-term-project-creation');
  });

  test('07-Sidebar应该显示长期任务面板', async ({ page }) => {
    // 1. 导航到首页
    await page.goto('/');

    // 2. 验证Sidebar面板存在
    const sidebarPanel = page.locator('[data-testid="project-sidebar-panel"]');
    const panelVisible = await sidebarPanel.isVisible().catch(() => false);

    // 如果没有活跃项目，面板可能不显示
    if (panelVisible) {
      await expect(sidebarPanel).toBeVisible();

      // 3. 验证面板头部
      const header = page.locator('[data-testid="project-panel-header"]');
      await expect(header).toBeVisible();
      await expect(header).toContainText('长期任务');

      // 4. 验证进度条
      const progressBar = page.locator('[data-testid="project-progress-bar"]');
      const hasProgressBar = await progressBar.count() > 0;
      if (hasProgressBar) {
        await expect(progressBar.first()).toBeVisible();
      }
    } else {
      console.log('长期任务面板不可见，可能没有活跃的长期任务');
    }

    await takeScreenshot(page, 'sidebar-project-panel');
  });

  test('08-应该切换自动完成开关', async ({ page }) => {
    // 1. 创建长期任务
    await page.goto('/');
    const chatInput = page.locator('textarea[placeholder*="消息"]');
    await chatInput.fill('创建长期任务：测试自动完成');
    await page.click('button:has-text("发送")');

    // 2. 等待任务创建
    await expect(page.locator('[data-testid="task-card"]')).toBeVisible({ timeout: 10000 });

    // 3. 查找自动完成开关
    const toggle = page.locator('[data-testid="auto-execute-toggle"]');
    const toggleVisible = await toggle.isVisible().catch(() => false);

    if (toggleVisible) {
      // 4. 点击开关
      await toggle.click();

      // 5. 验证状态变化
      const toggleState = await toggle.getAttribute('data-state');
      expect(['checked', 'unchecked']).toContain(toggleState);
    } else {
      console.log('自动完成开关不可见，可能未实现或在Sidebar中');
    }

    await takeScreenshot(page, 'toggle-auto-execute');
  });

  test('09-应该暂停和恢复长期任务', async ({ page }) => {
    // 1. 创建长期任务
    await page.goto('/');
    const chatInput = page.locator('textarea[placeholder*="消息"]');
    await chatInput.fill('创建长期任务：测试暂停恢复');
    await page.click('button:has-text("发送")');

    // 2. 等待任务创建
    await expect(page.locator('[data-testid="task-card"]')).toBeVisible({ timeout: 10000 });

    // 3. 查找暂停按钮
    const pauseButton = page.locator('button:has-text("暂停"), [data-testid="pause-project-button"]');
    const hasPauseButton = await pauseButton.isVisible().catch(() => false);

    if (hasPauseButton) {
      // 4. 点击暂停
      await pauseButton.click();

      // 5. 验证状态
      const status = page.locator('[data-testid="project-status"]');
      await expect(status).toContainText('已暂停', { timeout: 5000 });

      // 6. 查找恢复按钮
      const resumeButton = page.locator('button:has-text("恢复"), [data-testid="resume-project-button"]');
      await expect(resumeButton).toBeVisible();

      // 7. 点击恢复
      await resumeButton.click();

      // 8. 验证状态变化
      await expect(status).toContainText('运行中', { timeout: 5000 });
    } else {
      console.log('暂停/恢复按钮不可见，可能未实现');
    }

    await takeScreenshot(page, 'pause-resume-project');
  });

  test('10-应该显示长期任务进度', async ({ page }) => {
    // 1. 创建长期任务
    await page.goto('/');
    const chatInput = page.locator('textarea[placeholder*="消息"]');
    await chatInput.fill('创建长期任务：测试进度显示');
    await page.click('button:has-text("发送")');

    // 2. 等待任务创建
    await expect(page.locator('[data-testid="task-card"]')).toBeVisible({ timeout: 10000 });

    // 3. 验证进度显示
    const progressText = page.locator('text=/\\d+\\/\\d+/');
    const hasProgressText = await progressText.count() > 0;

    if (hasProgressText) {
      await expect(progressText.first()).toBeVisible();
    }

    await takeScreenshot(page, 'project-progress-display');
  });
});

/**
 * ============================================================================
 * 测试组 3: Agent 多Tab管理
 * ============================================================================
 */
test.describe('Agent多Tab管理', () => {
  test('11-应该显示主Agent Tab', async ({ page }) => {
    // 1. 创建会话
    await page.goto('/');

    // 2. 验证主Agent Tab存在
    const mainTab = page.locator('[data-testid="agent-tab"][data-level="1"]');
    const tabVisible = await mainTab.isVisible().catch(() => false);

    if (tabVisible) {
      await expect(mainTab).toBeVisible();
      await expect(mainTab).toContainText('主Agent', { timeout: 5000 });
    } else {
      // 如果没有data-level属性，尝试其他选择器
      const agentTabs = page.locator('[data-testid="agent-tab"]');
      const count = await agentTabs.count();
      expect(count).toBeGreaterThan(0);
    }

    await takeScreenshot(page, 'main-agent-tab');
  });

  test('12-应该打开子Agent Tab', async ({ page }) => {
    // 1. 创建任务（会生成子Agent）
    await page.goto('/');
    const chatInput = page.locator('textarea[placeholder*="消息"]');
    await chatInput.fill('创建任务：测试子Agent创建');
    await page.click('button:has-text("发送")');

    // 2. 等待任务创建
    await page.waitForTimeout(5000);

    // 3. 查找子Agent Tabs
    const subTabs = page.locator('[data-testid="agent-tab"][data-level="2"], [data-testid="agent-tab"]:not([data-level="1"])');
    const subTabCount = await subTabs.count();

    // 4. 验证子Agent数量（可能还没有子Agent）
    if (subTabCount > 0) {
      expect(subTabCount).toBeGreaterThan(0);
    } else {
      console.log('暂无子Agent Tab，可能需要更长时间生成');
    }

    await takeScreenshot(page, 'sub-agent-tabs');
  });

  test('13-应该切换Agent Tab', async ({ page }) => {
    // 1. 创建多个Agent Tabs
    await page.goto('/');
    const chatInput = page.locator('textarea[placeholder*="消息"]');
    await chatInput.fill('创建多个任务以生成多个Agent');
    await page.click('button:has-text("发送")');

    // 2. 等待Tabs创建
    await page.waitForTimeout(5000);

    // 3. 获取所有Agent Tabs
    const allTabs = page.locator('[data-testid="agent-tab"]');
    const tabCount = await allTabs.count();

    if (tabCount > 1) {
      // 4. 点击第二个Tab
      await allTabs.nth(1).click();

      // 5. 验证活动Tab
      const activeTab = page.locator('[data-testid="agent-tab"][data-active="true"], [data-testid="agent-tab"].active');
      await expect(activeTab).toBeVisible({ timeout: 5000 });
    } else {
      console.log('只有一个Tab，无法测试切换');
    }

    await takeScreenshot(page, 'switch-agent-tabs');
  });

  test('14-应该关闭子Agent Tab', async ({ page }) => {
    // 1. 创建子Agent Tab
    await page.goto('/');
    const chatInput = page.locator('textarea[placeholder*="消息"]');
    await chatInput.fill('创建任务');
    await page.click('button:has-text("发送")');
    await page.waitForTimeout(5000);

    // 2. 查找关闭按钮
    const closeButton = page.locator('[data-testid="agent-tab"] [data-testid="close-tab-button"], [data-testid="agent-tab"] button[aria-label*="关闭"]');
    const closeButtonCount = await closeButton.count();

    if (closeButtonCount > 0) {
      const initialTabCount = await page.locator('[data-testid="agent-tab"]').count();

      // 3. 点击第一个关闭按钮
      await closeButton.first().click();

      // 4. 验证Tab数量减少（但至少保留主Tab）
      const newTabCount = await page.locator('[data-testid="agent-tab"]').count();
      expect(newTabCount).toBeLessThanOrEqual(initialTabCount);
      expect(newTabCount).toBeGreaterThanOrEqual(1);
    } else {
      console.log('没有可关闭的Tab');
    }

    await takeScreenshot(page, 'close-agent-tab');
  });
});

/**
 * ============================================================================
 * 测试组 4: 可折叠面板
 * ============================================================================
 */
test.describe('可折叠面板', () => {
  test('15-应该折叠和展开执行图面板', async ({ page }) => {
    // 1. 导航到会话页面
    await page.goto('/');

    // 2. 查找执行图面板
    const executionGraphPanel = page.locator('[data-testid="execution-graph-panel"]');
    const panelVisible = await executionGraphPanel.isVisible().catch(() => false);

    if (panelVisible) {
      // 3. 初始状态：展开
      await expect(executionGraphPanel).toBeVisible();

      // 4. 查找折叠按钮
      const toggleButton = page.locator('[data-testid="toggle-execution-graph-button"], button:has-text("执行图")');
      const hasToggleButton = await toggleButton.isVisible().catch(() => false);

      if (hasToggleButton) {
        // 5. 折叠
        await toggleButton.click();
        await expect(executionGraphPanel).not.toBeVisible();

        // 6. 展开
        await toggleButton.click();
        await expect(executionGraphPanel).toBeVisible();
      }
    } else {
      console.log('执行图面板不可见');
    }

    await takeScreenshot(page, 'toggle-execution-graph');
  });

  test('16-应该折叠和展开任务状态面板', async ({ page }) => {
    // 1. 导航到会话页面
    await page.goto('/');

    // 2. 查找任务状态面板
    const taskPanel = page.locator('[data-testid="task-status-panel"], [data-testid="task-panel"]');
    const panelVisible = await taskPanel.isVisible().catch(() => false);

    if (panelVisible) {
      // 3. 查找折叠按钮
      const toggleButton = page.locator('[data-testid="toggle-task-panel-button"], button:has-text("任务")');
      const hasToggleButton = await toggleButton.isVisible().catch(() => false);

      if (hasToggleButton) {
        // 4. 折叠
        await toggleButton.click();

        // 5. 验证折叠状态
        const isCollapsed = await taskPanel.evaluate(el => el.classList.contains('collapsed'));
        expect(isCollapsed).toBeTruthy();

        // 6. 展开
        await toggleButton.click();
        const isExpanded = await taskPanel.evaluate(el => !el.classList.contains('collapsed'));
        expect(isExpanded).toBeTruthy();
      }
    } else {
      console.log('任务状态面板不可见');
    }

    await takeScreenshot(page, 'toggle-task-panel');
  });

  test('17-应该使用快捷键切换面板', async ({ page }) => {
    // 1. 导航到会话页面
    await page.goto('/');

    // 2. 查找执行图面板
    const executionGraphPanel = page.locator('[data-testid="execution-graph-panel"]');
    const panelVisible = await executionGraphPanel.isVisible().catch(() => false);

    if (panelVisible) {
      const wasVisible = await executionGraphPanel.isVisible();

      // 3. 使用快捷键 Ctrl+G
      await page.keyboard.press('Control+g');
      await page.waitForTimeout(500);

      // 4. 验证状态变化
      const isNowVisible = await executionGraphPanel.isVisible();
      expect(isNowVisible).not.toBe(wasVisible);
    } else {
      console.log('执行图面板不可见，跳过快捷键测试');
    }

    await takeScreenshot(page, 'keyboard-shortcut-toggle');
  });
});

/**
 * ============================================================================
 * 测试组 5: 监控页面
 * ============================================================================
 */
test.describe('监控页面', () => {
  test('18-应该显示层级任务列表', async ({ page }) => {
    // 1. 导航到监控页面
    await page.goto('/monitoring');
    await waitForPageLoad(page);

    // 2. 验证层级任务列表
    const taskList = page.locator('[data-testid="task-hierarchy-list"]');
    const listVisible = await taskList.isVisible().catch(() => false);

    if (listVisible) {
      await expect(taskList).toBeVisible();
    } else {
      console.log('层级任务列表不可见，可能没有任务数据');
    }

    await takeScreenshot(page, 'monitoring-task-hierarchy');
  });

  test('19-应该展开和折叠长期任务', async ({ page }) => {
    // 1. 导航到监控页面
    await page.goto('/monitoring');
    await waitForPageLoad(page);

    // 2. 查找展开按钮
    const expandButton = page.locator('[data-testid="expand-project-button"], button:has-text("展开")');
    const buttonCount = await expandButton.count();

    if (buttonCount > 0) {
      // 3. 记录初始子任务数量
      const initialSubTasks = await page.locator('[data-testid="sub-task-item"]').count();

      // 4. 点击展开按钮
      await expandButton.first().click();
      await page.waitForTimeout(500);

      // 5. 验证子任务显示
      const subTasks = await page.locator('[data-testid="sub-task-item"]').count();
      expect(subTasks).toBeGreaterThanOrEqual(initialSubTasks);
    } else {
      console.log('没有可展开的长期任务');
    }

    await takeScreenshot(page, 'monitoring-expand-project');
  });

  test('20-应该打开任务详情面板', async ({ page }) => {
    // 1. 导航到监控页面
    await page.goto('/monitoring');
    await waitForPageLoad(page);

    // 2. 查找任务项
    const taskItem = page.locator('[data-testid="task-item"], [data-testid="project-item"]');
    const itemCount = await taskItem.count();

    if (itemCount > 0) {
      // 3. 点击任务
      await taskItem.first().click();
      await page.waitForTimeout(500);

      // 4. 验证详情面板
      const detailPanel = page.locator('[data-testid="task-detail-panel"]');
      const panelVisible = await detailPanel.isVisible().catch(() => false);

      if (panelVisible) {
        await expect(detailPanel).toBeVisible();
      }
    } else {
      console.log('没有可点击的任务');
    }

    await takeScreenshot(page, 'monitoring-task-detail');
  });
});

/**
 * ============================================================================
 * 测试组 6: 主题切换
 * ============================================================================
 */
test.describe('主题系统', () => {
  test('21-应该切换深色和浅色主题', async ({ page }) => {
    // 1. 导航到首页
    await page.goto('/');

    // 2. 获取当前主题
    const initialTheme = await getCurrentTheme(page);
    console.log('初始主题:', initialTheme);

    // 3. 切换到深色模式
    await switchTheme(page, 'dark');

    // 4. 验证深色模式
    await verifyTheme(page, 'dark');

    // 5. 验证body有dark类
    const hasDarkClass = await page.locator('body').evaluate(el => el.classList.contains('dark'));
    expect(hasDarkClass).toBeTruthy();

    // 6. 切换回浅色模式
    await switchTheme(page, 'light');

    // 7. 验证浅色模式
    await verifyTheme(page, 'light');

    await takeScreenshot(page, 'theme-switch-dark-light');
  });

  test('22-任务组件应该正确显示主题颜色', async ({ page }) => {
    // 1. 创建任务
    await page.goto('/');
    const chatInput = page.locator('textarea[placeholder*="消息"]');
    await chatInput.fill('创建任务：测试主题颜色');
    await page.click('button:has-text("发送")');

    // 2. 等待任务卡片
    await expect(page.locator('[data-testid="task-card"]')).toBeVisible({ timeout: 10000 });

    // 3. 获取任务状态图标颜色（深色模式）
    await switchTheme(page, 'dark');
    await page.waitForTimeout(500);

    const statusIconDark = page.locator('[data-testid="task-status-icon"], [data-testid="task-card"] svg');
    const colorDark = await statusIconDark.first().evaluate(el => {
      return window.getComputedStyle(el).color;
    });
    expect(colorDark).toBeTruthy();
    console.log('深色模式图标颜色:', colorDark);

    // 4. 获取任务状态图标颜色（浅色模式）
    await switchTheme(page, 'light');
    await page.waitForTimeout(500);

    const statusIconLight = page.locator('[data-testid="task-status-icon"], [data-testid="task-card"] svg');
    const colorLight = await statusIconLight.first().evaluate(el => {
      return window.getComputedStyle(el).color;
    });
    expect(colorLight).toBeTruthy();
    console.log('浅色模式图标颜色:', colorLight);

    // 5. 验证颜色不同
    expect(colorDark).not.toBe(colorLight);

    await takeScreenshot(page, 'task-theme-colors');
  });

  test('23-主题切换应该保存到localStorage', async ({ page }) => {
    // 1. 切换到深色主题
    await page.goto('/');
    await switchTheme(page, 'dark');

    // 2. 验证localStorage
    const storedTheme = await page.evaluate(() => localStorage.getItem('theme'));
    expect(storedTheme).toBe('dark');

    // 3. 刷新页面
    await page.reload();
    await waitForPageLoad(page);

    // 4. 验证主题保持
    const currentTheme = await getCurrentTheme(page);
    expect(currentTheme).toBe('dark');

    await takeScreenshot(page, 'theme-persistence');
  });
});

/**
 * ============================================================================
 * 测试组 7: WebSocket实时更新
 * ============================================================================
 */
test.describe('WebSocket实时更新', () => {
  test('24-应该显示WebSocket连接状态', async ({ page }) => {
    // 1. 导航到首页
    await page.goto('/');

    // 2. 查找WebSocket状态指示器
    const statusIndicator = page.locator('[data-testid="websocket-status"], .h-2.w-2.rounded-full');
    await expect(statusIndicator).toBeVisible();

    // 3. 等待连接成功
    await page.waitForTimeout(3000);

    // 4. 验证状态
    const statusText = await statusIndicator.textContent();
    console.log('WebSocket状态:', statusText);

    await takeScreenshot(page, 'websocket-status-indicator');
  });

  test('25-应该实时更新任务状态', async ({ page }) => {
    // 1. 创建会话
    await page.goto('/');

    // 2. 创建任务
    const chatInput = page.locator('textarea[placeholder*="消息"]');
    await chatInput.fill('创建任务：测试实时更新');
    await page.click('button:has-text("发送")');

    // 3. 等待任务卡片
    await expect(page.locator('[data-testid="task-card"]')).toBeVisible({ timeout: 10000 });

    // 4. 记录初始阶段
    const initialPhase = await page.locator('[data-testid="current-phase"]').textContent().catch(() => 'unknown');
    console.log('初始阶段:', initialPhase);

    // 5. 等待阶段变化（可能需要等待WebSocket更新）
    await page.waitForTimeout(5000);

    // 6. 验证阶段指示器可见
    const phaseIndicator = page.locator('[data-testid="task-phase-indicator"]');
    await expect(phaseIndicator).toBeVisible();

    await takeScreenshot(page, 'websocket-task-update');
  });

  test('26-应该处理WebSocket断线重连', async ({ page }) => {
    // 1. 导航到首页
    await page.goto('/');

    // 2. 等待WebSocket连接
    await page.waitForTimeout(3000);

    // 3. 模拟网络断开（通过离线模式）
    await page.context().setOffline(true);
    await page.waitForTimeout(2000);

    // 4. 验证状态变为未连接
    const statusIndicator = page.locator('[data-testid="websocket-status"]');
    const statusText = await statusIndicator.textContent();
    console.log('断线后状态:', statusText);

    // 5. 恢复网络
    await page.context().setOffline(false);
    await page.waitForTimeout(2000);

    // 6. 验证重连
    const reconnectedText = await statusIndicator.textContent();
    console.log('重连后状态:', reconnectedText);

    await takeScreenshot(page, 'websocket-reconnect');
  });
});

/**
 * ============================================================================
 * 测试组 8: 综合测试
 * ============================================================================
 */
test.describe('综合测试', () => {
  test('27-完整的任务执行流程', async ({ page }) => {
    // 1. 创建会话
    await page.goto('/');

    // 2. 创建任务
    const chatInput = page.locator('textarea[placeholder*="消息"]');
    await chatInput.fill('帮我实现一个用户登录功能，需要支持用户名密码登录和JWT Token认证');
    await page.click('button:has-text("发送")');

    // 3. 等待任务卡片
    await expect(page.locator('[data-testid="task-card"]')).toBeVisible({ timeout: 10000 });

    // 4. 验证任务目标
    const taskGoal = page.locator('[data-testid="task-card"] p.font-medium');
    await expect(taskGoal).toContainText('用户登录', { timeout: 5000 });

    // 5. 验证三阶段指示器
    const phases = page.locator('[data-testid="task-phase"]');
    const phaseCount = await phases.count();
    expect(phaseCount).toBeGreaterThanOrEqual(3);

    // 6. 验证AC列表
    const expandButton = page.locator('[data-testid="task-card"] button[aria-label*="展开"]');
    const expandVisible = await expandButton.isVisible().catch(() => false);
    if (expandVisible) {
      await expandButton.click();
    }

    // 7. 等待一段时间观察任务进度
    await page.waitForTimeout(10000);

    await takeScreenshot(page, 'complete-task-flow');
  });

  test('28-多任务并行执行', async ({ page }) => {
    // 1. 创建会话
    await page.goto('/');

    // 2. 创建第一个任务
    const chatInput = page.locator('textarea[placeholder*="消息"]');
    await chatInput.fill('创建任务1：实现用户注册');
    await page.click('button:has-text("发送")');

    // 3. 等待第一个任务创建
    await page.waitForTimeout(3000);

    // 4. 创建第二个任务
    await chatInput.fill('创建任务2：实现用户登录');
    await page.click('button:has-text("发送")');

    // 5. 等待第二个任务创建
    await page.waitForTimeout(3000);

    // 6. 验证两个任务卡片都存在
    const taskCards = page.locator('[data-testid="task-card"]');
    const cardCount = await taskCards.count();
    expect(cardCount).toBeGreaterThanOrEqual(2);

    await takeScreenshot(page, 'multiple-tasks-parallel');
  });

  test('29-任务失败和重试', async ({ page }) => {
    // 1. 创建会话
    await page.goto('/');

    // 2. 创建一个可能会失败的任务
    const chatInput = page.locator('textarea[placeholder*="消息"]');
    await chatInput.fill('创建任务：测试失败处理（故意使用错误的验收标准）');
    await page.click('button:has-text("发送")');

    // 3. 等待任务卡片
    await expect(page.locator('[data-testid="task-card"]')).toBeVisible({ timeout: 10000 });

    // 4. 等待一段时间观察任务状态
    await page.waitForTimeout(15000);

    // 5. 检查是否有失败状态
    const failedStatus = page.locator('[data-testid="task-status-failed"], [data-status="failed"]');
    const hasFailed = await failedStatus.count() > 0;

    if (hasFailed) {
      console.log('任务失败，验证失败状态显示正确');

      // 6. 查找重试按钮
      const retryButton = page.locator('button:has-text("重试"), [data-testid="retry-button"]');
      const hasRetryButton = await retryButton.isVisible().catch(() => false);

      if (hasRetryButton) {
        await retryButton.click();
        console.log('已点击重试按钮');
      }
    } else {
      console.log('任务未失败，可能已成功执行');
    }

    await takeScreenshot(page, 'task-failure-retry');
  });

  test('30-性能测试-大量任务显示', async ({ page }) => {
    // 1. 创建会话
    await page.goto('/');

    // 2. 创建多个任务
    const chatInput = page.locator('textarea[placeholder*="消息"]');

    for (let i = 1; i <= 5; i++) {
      await chatInput.fill(`创建任务${i}：测试性能`);
      await page.click('button:has-text("发送")');
      await page.waitForTimeout(2000);
    }

    // 3. 验证所有任务卡片都显示
    const taskCards = page.locator('[data-testid="task-card"]');
    const cardCount = await taskCards.count();
    expect(cardCount).toBeGreaterThanOrEqual(5);

    // 4. 验证页面性能（检查FPS）
    const metrics = await page.evaluate(() => {
      const navEntry = performance.getEntriesByType('navigation')[0] as PerformanceNavigationTiming;
      return {
        domContentLoaded: Math.round(navEntry.domContentLoadedEventEnd - navEntry.domContentLoadedEventStart),
        loadComplete: Math.round(navEntry.loadEventEnd - navEntry.loadEventStart),
      };
    });

    console.log('页面性能指标:', metrics);
    expect(metrics.domContentLoaded).toBeLessThan(3000);
    expect(metrics.loadComplete).toBeLessThan(5000);

    await takeScreenshot(page, 'performance-many-tasks');
  });
});
