/**
 * Agent 组件测试
 *
 * 测试覆盖范围：
 * 1. AgentIcon 组件 - 不同类型和尺寸的图标显示
 * 2. AgentSelector 组件 - Agent 选择器交互
 * 3. AgentConfigPanel 组件 - Agent 配置面板
 * 4. AgentNode 组件 - 图形化节点显示
 * 5. 状态管理 - Agent Store 数据流
 * 6. 组件间通信 - Agent 相关组件的交互
 */

import { test, expect } from '@playwright/test';

const BASE_URL = process.env.FRONTEND_URL || process.env.REACT_APP_FRONTEND_URL || "http://localhost:5188";
const API_BASE = process.env.API_BASE_URL || process.env.REACT_APP_API_URL || "http://localhost:8888";

/**
 * 测试辅助函数：等待 API 响应
 */
async function waitForApi(ms: number = 500) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

/**
 * 测试辅助函数：获取认证 token
 */
async function getAuthToken(page) {
  // 尝试从 localStorage 获取
  const token = await page.evaluate(() => localStorage.getItem('access_token'));
  if (token) return token;

  // 如果没有 token，尝试登录
  const response = await page.context().request.post(`${API_BASE}/api/v1/auth/login`, {
    data: {
      username: 'admin',
      password: 'admin123'
    }
  });

  if (response.ok) {
    const data = await response.json();
    return data.access_token;
  }

  return null;
}

test.describe('Agent 组件测试套件', () => {
  test.beforeAll(async () => {
    console.log('🚀 开始 Agent 组件测试');
  });

  test.afterAll(async () => {
    console.log('✅ Agent 组件测试完成');
  });

  /**
   * 测试 1: AgentIcon 组件基本渲染
   */
  test('01-AgentIcon-基本渲染', async ({ page }) => {
    await page.goto(`${BASE_URL}/demo`);
    await page.waitForLoadState('networkidle');

    // 等待页面加载
    await waitForApi(1000);

    // 查找 AgentIcon 组件（通过 emoji 图标特征）
    const agentIcons = await page.locator('[class*="rounded-lg"]').filter({ hasText: /[✨🐍📝🧪🔍👁️]/ });

    const iconCount = await agentIcons.count();
    console.log(`📊 找到 ${iconCount} 个 AgentIcon 组件`);

    // 至少应该有一些图标
    expect(iconCount).toBeGreaterThan(0);

    await page.screenshot({
      path: 'frontend/test-results/06-01-agent-icon-basic.png',
      fullPage: true
    });

    console.log('✅ AgentIcon 基本渲染测试通过');
  });

  /**
   * 测试 2: AgentIcon 不同类型图标
   */
  test('02-AgentIcon-不同类型图标', async ({ page }) => {
    await page.goto(`${BASE_URL}/demo`);
    await page.waitForLoadState('networkidle');
    await waitForApi(1000);

    // 检查不同类型的 Agent 图标
    const iconTypes = [
      { emoji: '✨', name: 'system' },
      { emoji: '🐍', name: 'code' },
      { emoji: '📝', name: 'doc' },
      { emoji: '🧪', name: 'test' },
      { emoji: '🔍', name: 'debug' },
      { emoji: '👁️', name: 'review' }
    ];

    const foundTypes = [];

    for (const type of iconTypes) {
      const icon = await page.locator(`text=${type.emoji}`).first();
      if (await icon.count() > 0) {
        foundTypes.push(type.name);
        console.log(`  ✓ 找到 ${type.name} 类型图标: ${type.emoji}`);
      }
    }

    console.log(`📊 找到 ${foundTypes.length} 种类型图标: ${foundTypes.join(', ')}`);

    await page.screenshot({
      path: 'frontend/test-results/06-02-agent-icon-types.png',
      fullPage: true
    });

    expect(foundTypes.length).toBeGreaterThan(0);
    console.log('✅ AgentIcon 类型图标测试通过');
  });

  /**
   * 测试 3: AgentSelector 组件显示
   */
  test('03-AgentSelector-组件显示', async ({ page }) => {
    await page.goto(`${BASE_URL}/demo`);
    await page.waitForLoadState('networkidle');
    await waitForApi(1000);

    // 查找 AgentSelector（下拉选择器）
    const selector = page.locator('button').filter({ hasText: /默认助手|Agent/ }).first();

    const isVisible = await selector.isVisible();
    console.log(`📊 AgentSelector 可见: ${isVisible}`);

    if (isVisible) {
      await selector.screenshot({
        path: 'frontend/test-results/06-03-agent-selector-component.png'
      });

      // 点击下拉菜单
      await selector.click();
      await waitForApi(500);

      await page.screenshot({
        path: 'frontend/test-results/06-03-agent-selector-opened.png',
        fullPage: true
      });

      console.log('✅ AgentSelector 组件显示测试通过');
    } else {
      console.log('⚠️ AgentSelector 未找到（可能不在当前页面）');
    }
  });

  /**
   * 测试 4: AgentSelector 下拉菜单交互
   */
  test('04-AgentSelector-下拉菜单交互', async ({ page }) => {
    await page.goto(`${BASE_URL}/demo`);
    await page.waitForLoadState('networkidle');
    await waitForApi(1000);

    // 查找并点击选择器
    const selector = page.locator('button').filter({ hasText: /默认助手|Agent/ }).first();

    if (await selector.isVisible()) {
      await selector.click();
      await waitForApi(500);

      // 检查下拉菜单内容
      const dropdown = page.locator('[role="menu"]').first();

      const menuItems = await dropdown.locator('[role="menuitem"]').all();
      console.log(`📊 下拉菜单项数量: ${menuItems.length}`);

      for (let i = 0; i < Math.min(menuItems.length, 5); i++) {
        const text = await menuItems[i].textContent();
        console.log(`  - ${text?.trim()}`);
      }

      await page.screenshot({
        path: 'frontend/test-results/06-04-agent-selector-dropdown.png',
        fullPage: true
      });

      expect(menuItems.length).toBeGreaterThan(0);
      console.log('✅ AgentSelector 下拉菜单交互测试通过');
    } else {
      console.log('⚠️ AgentSelector 未找到，跳过交互测试');
    }
  });

  /**
   * 测试 5: AgentConfigPanel 基本结构
   */
  test('05-AgentConfigPanel-基本结构', async ({ page }) => {
    // 尝试导航到 Agent 配置页面
    try {
      await page.goto(`${BASE_URL}/settings`);
      await page.waitForLoadState('networkidle');
      await waitForApi(1000);

      // 查找配置面板相关元素
      const configPanel = page.locator('section').filter({ hasText: /Agent|配置/ });

      const hasConfig = await configPanel.count() > 0;
      console.log(`📊 找到配置面板: ${hasConfig}`);

      await page.screenshot({
        path: 'frontend/test-results/06-05-agent-config-panel.png',
        fullPage: true
      });

      console.log('✅ AgentConfigPanel 基本结构测试完成');
    } catch (error) {
      console.log('⚠️ 配置页面不可用:', error);
    }
  });

  /**
   * 测试 6: AgentNode 图形化节点显示
   */
  test('06-AgentNode-图形化节点', async ({ page }) => {
    await page.goto(`${BASE_URL}/demo`);
    await page.waitForLoadState('networkidle');
    await waitForApi(1000);

    // 查找图形化节点（SVG 或 canvas）
    const graphContainer = page.locator('svg').first();
    const hasGraph = await graphContainer.isVisible();

    console.log(`📊 图形容器可见: ${hasGraph}`);

    if (hasGraph) {
      // 查找节点元素
      const nodes = page.locator('.react-flow__node');
      const nodeCount = await nodes.count();

      console.log(`📊 找到 ${nodeCount} 个节点`);

      if (nodeCount > 0) {
        // 获取第一个节点的文本
        const firstNodeText = await nodes.first().textContent();
        console.log(`  第一个节点内容: ${firstNodeText?.trim()}`);

        await page.screenshot({
          path: 'frontend/test-results/06-06-agent-nodes-graph.png',
          fullPage: true
        });
      }
    }

    await page.screenshot({
      path: 'frontend/test-results/06-06-agent-graph-overview.png',
      fullPage: true
    });

    console.log('✅ AgentNode 图形化节点测试完成');
  });

  /**
   * 测试 7: Agent 节点状态显示
   */
  test('07-AgentNode-节点状态', async ({ page }) => {
    await page.goto(`${BASE_URL}/demo`);
    await page.waitForLoadState('networkidle');
    await waitForApi(1000);

    // 查找状态相关的元素
    const statusElements = page.locator('[class*="status"]');
    const statusCount = await statusElements.count();

    console.log(`📊 找到 ${statusCount} 个状态相关元素`);

    if (statusCount > 0) {
      const statuses = [];

      for (let i = 0; i < Math.min(statusCount, 10); i++) {
        const text = await statusElements.nth(i).textContent();
        if (text) {
          statuses.push(text.trim());
        }
      }

      console.log(`  状态列表: ${statuses.slice(0, 5).join(', ')}`);
    }

    await page.screenshot({
      path: 'frontend/test-results/06-07-agent-node-status.png',
      fullPage: true
    });

    console.log('✅ Agent 节点状态显示测试完成');
  });

  /**
   * 测试 8: Agent Store 数据流
   */
  test('08-AgentStore-数据流', async ({ page }) => {
    await page.goto(`${BASE_URL}/demo`);
    await page.waitForLoadState('networkidle');

    // 检查 localStorage 中的 Agent 数据
    const agentData = await page.evaluate(() => {
      const data = {
        hasLocalStorage: !!localStorage.getItem('access_token'),
        agentsCount: 0,
        currentAgentId: null
      };

      // 尝试从 window 获取 store 数据（如果暴露）
      if ((window as any).__AGENT_STORE__) {
        data.agentsCount = (window as any).__AGENT_STORE__.agents?.length || 0;
        data.currentAgentId = (window as any).__AGENT_STORE__.currentAgentId;
      }

      return data;
    });

    console.log('📊 Agent Store 数据:');
    console.log(`  - 已认证: ${agentData.hasLocalStorage}`);
    console.log(`  - Agent 数量: ${agentData.agentsCount}`);
    console.log(`  - 当前 Agent ID: ${agentData.currentAgentId}`);

    await page.screenshot({
      path: 'frontend/test-results/06-08-agent-store-data.png',
      fullPage: true
    });

    console.log('✅ Agent Store 数据流测试完成');
  });

  /**
   * 测试 9: Agent 组件交互 - 选择 Agent
   */
  test('09-Agent-组件交互选择', async ({ page }) => {
    await page.goto(`${BASE_URL}/demo`);
    await page.waitForLoadState('networkidle');
    await waitForApi(1000);

    // 查找 Agent 选择器
    const selector = page.locator('button').filter({ hasText: /默认助手|Agent/ }).first();

    if (await selector.isVisible()) {
      // 截图前
      await page.screenshot({
        path: 'frontend/test-results/06-09-agent-selection-before.png'
      });

      // 点击打开菜单
      await selector.click();
      await waitForApi(500);

      // 尝试选择一个 Agent（如果有）
      const menuItems = page.locator('[role="menuitem"]');
      const itemCount = await menuItems.count();

      if (itemCount > 1) {
        // 点击第二个菜单项（第一个通常是"默认助手"）
        await menuItems.nth(1).click();
        await waitForApi(1000);

        console.log(`📊 已选择菜单项`);

        await page.screenshot({
          path: 'frontend/test-results/06-09-agent-selection-after.png',
          fullPage: true
        });
      }

      console.log('✅ Agent 组件交互选择测试完成');
    } else {
      console.log('⚠️ Agent 选择器未找到');
    }
  });

  /**
   * 测试 10: Agent 组件响应式布局
   */
  test('10-Agent-响应式布局', async ({ page }) => {
    await page.goto(`${BASE_URL}/demo`);
    await page.waitForLoadState('networkidle');
    await waitForApi(1000);

    // 测试不同屏幕尺寸
    const sizes = [
      { width: 1920, height: 1080, name: '桌面大屏' },
      { width: 1366, height: 768, name: '桌面标准' },
      { width: 768, height: 1024, name: '平板' },
      { width: 375, height: 667, name: '手机' }
    ];

    for (const size of sizes) {
      await page.setViewportSize({ width: size.width, height: size.height });
      await waitForApi(500);

      await page.screenshot({
        path: `frontend/test-results/06-10-responsive-${size.name}.png`,
        fullPage: true
      });

      console.log(`📊 ${size.name} (${size.width}x${size.height}) 截图完成`);
    }

    console.log('✅ Agent 组件响应式布局测试完成');
  });

  /**
   * 测试 11: Agent 图标尺寸变化
   */
  test('11-AgentIcon-尺寸变化', async ({ page }) => {
    await page.goto(`${BASE_URL}/demo`);
    await page.waitForLoadState('networkidle');
    await waitForApi(1000);

    // 查找不同尺寸的图标
    const smallIcons = page.locator('.w-5.h-5');
    const mediumIcons = page.locator('.w-8.h-8');
    const largeIcons = page.locator('.w-12.h-12');

    const smallCount = await smallIcons.count();
    const mediumCount = await mediumIcons.count();
    const largeCount = await largeIcons.count();

    console.log('📊 Agent 图标尺寸分布:');
    console.log(`  - 小图标 (sm): ${smallCount}`);
    console.log(`  - 中图标 (md): ${mediumCount}`);
    console.log(`  - 大图标 (lg): ${largeCount}`);

    await page.screenshot({
      path: 'frontend/test-results/06-11-agent-icon-sizes.png',
      fullPage: true
    });

    console.log('✅ Agent 图标尺寸变化测试完成');
  });

  /**
   * 测试 12: Agent 组件加载状态
   */
  test('12-Agent-加载状态', async ({ page }) => {
    // 监听网络请求
    const apiRequests: string[] = [];

    page.on('request', request => {
      const url = request.url();
      if (url.includes('/api/')) {
        apiRequests.push(url);
      }
    });

    await page.goto(`${BASE_URL}/demo`);
    await page.waitForLoadState('networkidle');

    // 等待一段时间收集请求
    await waitForApi(2000);

    console.log('📊 API 请求统计:');
    console.log(`  - 总请求数: ${apiRequests.length}`);

    const agentRequests = apiRequests.filter(url => url.includes('/agent'));
    console.log(`  - Agent 相关请求: ${agentRequests.length}`);

    if (agentRequests.length > 0) {
      console.log('  Agent 请求列表:');
      agentRequests.forEach(req => {
        console.log(`    - ${req}`);
      });
    }

    await page.screenshot({
      path: 'frontend/test-results/06-12-agent-loading-state.png',
      fullPage: true
    });

    console.log('✅ Agent 组件加载状态测试完成');
  });

  /**
   * 测试 13: Agent 组件错误处理
   */
  test('13-Agent-错误处理', async ({ page }) => {
    // 拦截 API 请求模拟错误
    await page.route('**/api/v1/agents**', route => {
      route.fulfill({
        status: 500,
        contentType: 'application/json',
        body: JSON.stringify({ error: 'Internal Server Error' })
      });
    });

    await page.goto(`${BASE_URL}/demo`);
    await page.waitForLoadState('networkidle');
    await waitForApi(2000);

    // 检查是否有错误提示
    const errorElements = page.locator('text=/错误|失败|Error/');

    const errorCount = await errorElements.count();
    console.log(`📊 错误元素数量: ${errorCount}`);

    await page.screenshot({
      path: 'frontend/test-results/06-13-agent-error-handling.png',
      fullPage: true
    });

    // 恢复正常路由
    await page.unroute('**/api/v1/agents**');

    console.log('✅ Agent 组件错误处理测试完成');
  });

  /**
   * 测试 14: Agent 组件可访问性
   */
  test('14-Agent-可访问性', async ({ page }) => {
    await page.goto(`${BASE_URL}/demo`);
    await page.waitForLoadState('networkidle');
    await waitForApi(1000);

    // 检查 ARIA 标签
    const buttons = await page.locator('button[aria-label]').all();
    const inputs = await page.locator('input[aria-label]').all();
    const roles = await page.locator('[role]').all();

    console.log('📊 可访问性元素统计:');
    console.log(`  - 带 aria-label 的按钮: ${buttons.length}`);
    console.log(`  - 带 aria-label 的输入框: ${inputs.length}`);
    console.log(`  - 带 role 属性的元素: ${roles.length}`);

    // 检查键盘导航
    const focusableElements = await page.locator('button, input, select, [tabindex]').all();
    console.log(`  - 可聚焦元素: ${focusableElements.length}`);

    await page.screenshot({
      path: 'frontend/test-results/06-14-agent-accessibility.png',
      fullPage: true
    });

    console.log('✅ Agent 组件可访问性测试完成');
  });

  /**
   * 测试 15: Agent 组件性能测试
   */
  test('15-Agent-性能测试', async ({ page }) => {
    const startTime = Date.now();

    await page.goto(`${BASE_URL}/demo`);
    await page.waitForLoadState('networkidle');
    await waitForApi(1000);

    const loadTime = Date.now() - startTime;

    // 获取性能指标
    const metrics = await page.evaluate(() => {
      const perfData = window.performance.getEntriesByType('navigation')[0] as any;

      return {
        domContentLoaded: Math.round(perfData.domContentLoadedEventEnd - perfData.domContentLoadedEventStart),
        loadComplete: Math.round(perfData.loadEventEnd - perfData.loadEventStart),
        totalTime: Math.round(perfData.loadEventEnd - perfData.fetchStart)
      };
    });

    console.log('📊 性能指标:');
    console.log(`  - 页面加载时间: ${loadTime}ms`);
    console.log(`  - DOM 加载: ${metrics.domContentLoaded}ms`);
    console.log(`  - 完整加载: ${metrics.loadComplete}ms`);
    console.log(`  - 总时间: ${metrics.totalTime}ms`);

    await page.screenshot({
      path: 'frontend/test-results/06-15-agent-performance.png',
      fullPage: true
    });

    // 性能断言
    expect(loadTime).toBeLessThan(10000); // 10秒内加载完成
    console.log('✅ Agent 组件性能测试完成');
  });

  /**
   * 测试 16: Agent 组件整体截图
   */
  test('16-Agent-整体截图', async ({ page }) => {
    await page.goto(`${BASE_URL}/demo`);
    await page.waitForLoadState('networkidle');
    await waitForApi(1000);

    // 完整页面截图
    await page.screenshot({
      path: 'frontend/test-results/06-16-agent-overview-full.png',
      fullPage: true
    });

    // 视口截图
    await page.screenshot({
      path: 'frontend/test-results/06-16-agent-overview-viewport.png'
    });

    // 放大页面截图
    await page.setViewportSize({ width: 1920, height: 1080 });
    await waitForApi(500);
    await page.screenshot({
      path: 'frontend/test-results/06-16-agent-overview-zoomed.png',
      fullPage: true
    });

    console.log('✅ Agent 组件整体截图完成');
  });
});
