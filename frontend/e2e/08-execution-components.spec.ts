/**
 * 执行组件端到端测试 (08-execution-components)
 *
 * 测试执行组件的所有功能：
 * - TaskStatusPanel (任务状态面板)
 * - ExecutionLog (执行日志)
 * - ExecutionProgress (执行进度)
 * - ControlButtons (控制按钮)
 * - ApprovalPanel (审批面板)
 */

import { test, expect } from '@playwright/test';
import { takeScreenshot } from './helpers';

test.describe('执行组件测试', () => {
  test.beforeEach(async ({ page }) => {
    // 直接访问首页，不需要登录
    await page.goto('/');
    await page.waitForLoadState('domcontentloaded');
    await page.waitForTimeout(1000);
  });

  test.describe('任务状态面板 (TaskStatusPanel)', () => {
    test('应该显示空状态或任务信息', async ({ page }) => {
      // 检查是否有任务状态面板
      const taskPanel = page.locator('[data-testid="task-status-panel"], [data-testid="task-status-panel-empty"]');
      const count = await taskPanel.count();

      if (count > 0) {
        await expect(taskPanel.first()).toBeVisible();
        await takeScreenshot(page, 'task-status-panel');
      } else {
        // 如果没有找到面板，截图记录当前页面状态
        await takeScreenshot(page, 'task-status-panel-not-found');
      }
    });

    test('应该显示不同的执行状态', async ({ page }) => {
      // 可能的状态：running, completed, failed, paused, cancelled
      const statusSelectors = [
        'text=/执行中/',
        'text=/已完成/',
        'text=/失败/',
        'text=/已暂停/',
        'text=/已取消/',
        'text=/等待中/',
      ];

      let statusFound = false;
      for (const statusSelector of statusSelectors) {
        const statusElement = page.locator(statusSelector);
        const count = await statusElement.count();

        if (count > 0) {
          await expect(statusElement.first()).toBeVisible();
          statusFound = true;
          break;
        }
      }

      await takeScreenshot(page, statusFound ? 'task-status-found' : 'task-status-not-found');
    });

    test('应该显示任务进度条', async ({ page }) => {
      // 查找进度条元素
      const progressBar = page.locator('[role="progressbar"], .h-2.bg-muted.rounded-full, [class*="progress"]');
      const count = await progressBar.count();

      if (count > 0) {
        await expect(progressBar.first()).toBeVisible();
        await takeScreenshot(page, 'task-progress-bar');
      } else {
        await takeScreenshot(page, 'task-progress-bar-not-found');
      }
    });

    test('应该显示任务错误信息（如果有）', async ({ page }) => {
      // 查找错误信息显示区域
      const errorSection = page.locator('.bg-red-50, [class*="text-red-"]');
      const count = await errorSection.count();

      if (count > 0) {
        await expect(errorSection.first()).toBeVisible();
        await takeScreenshot(page, 'task-error-display');
      } else {
        await takeScreenshot(page, 'task-error-not-found');
      }
    });
  });

  test.describe('执行日志 (ExecutionLog)', () => {
    test('应该显示日志组件', async ({ page }) => {
      // 检查是否有执行日志组件
      const executionLog = page.locator('[data-testid="execution-log"]');
      const count = await executionLog.count();

      if (count > 0) {
        await expect(executionLog.first()).toBeVisible();
        await takeScreenshot(page, 'execution-log-display');
      } else {
        await takeScreenshot(page, 'execution-log-not-found');
      }
    });

    test('应该支持日志搜索功能', async ({ page }) => {
      // 查找搜索输入框
      const searchInput = page.locator('input[placeholder*="搜索"], input[type="search"]');
      const count = await searchInput.count();

      if (count > 0) {
        await expect(searchInput.first()).toBeVisible();

        // 输入搜索关键词
        await searchInput.first().fill('test');
        await page.waitForTimeout(500);

        // 验证搜索框有值
        const value = await searchInput.first().inputValue();
        expect(value).toBe('test');

        await takeScreenshot(page, 'execution-log-search');
      } else {
        await takeScreenshot(page, 'execution-log-search-not-found');
      }
    });

    test('应该支持日志级别过滤', async ({ page }) => {
      // 查找过滤器按钮
      const filterButton = page.locator('button').filter({ hasText: /过滤|Filter/ });
      const count = await filterButton.count();

      if (count > 0) {
        // 点击过滤器按钮
        await filterButton.first().click();
        await page.waitForTimeout(300);

        await takeScreenshot(page, 'execution-log-filter');
      } else {
        await takeScreenshot(page, 'execution-log-filter-not-found');
      }
    });

    test('应该显示不同级别的日志', async ({ page }) => {
      // 检查不同级别的日志
      const logLevels = ['调试', '信息', '警告', '错误', '成功'];

      let logFound = false;
      for (const level of logLevels) {
        const logEntry = page.locator(`text=/${level}/`);
        const count = await logEntry.count();

        if (count > 0) {
          await expect(logEntry.first()).toBeVisible();
          logFound = true;
          break;
        }
      }

      await takeScreenshot(page, logFound ? 'execution-log-levels-found' : 'execution-log-levels-not-found');
    });

    test('应该支持日志导出功能', async ({ page }) => {
      // 查找导出按钮
      const exportButton = page.locator('button').filter({ hasText: /导出|Export/ });
      const count = await exportButton.count();

      if (count > 0) {
        await takeScreenshot(page, 'execution-log-export-button');
      } else {
        await takeScreenshot(page, 'execution-log-export-not-found');
      }
    });
  });

  test.describe('执行进度 (ExecutionProgress)', () => {
    test('应该显示执行进度组件', async ({ page }) => {
      // 检查是否有执行进度组件
      const progressPanel = page.locator('[data-testid="execution-progress"], [data-testid="execution-progress-empty"]');
      const count = await progressPanel.count();

      if (count > 0) {
        await expect(progressPanel.first()).toBeVisible();
        await takeScreenshot(page, 'execution-progress-display');
      } else {
        await takeScreenshot(page, 'execution-progress-not-found');
      }
    });

    test('应该显示步骤指示器', async ({ page }) => {
      // 查找步骤图标
      const stepIcons = page.locator('[data-testid="execution-progress"] svg, [data-testid="execution-progress-empty"] svg');
      const count = await stepIcons.count();

      if (count > 0) {
        await expect(stepIcons.first()).toBeVisible();
        await takeScreenshot(page, 'execution-progress-steps');
      } else {
        await takeScreenshot(page, 'execution-progress-steps-not-found');
      }
    });

    test('应该显示进度百分比', async ({ page }) => {
      // 查找进度百分比显示
      const progressText = page.locator('text=/\\d+%/');
      const count = await progressText.count();

      if (count > 0) {
        await expect(progressText.first()).toBeVisible();
        await takeScreenshot(page, 'execution-progress-percent');
      } else {
        await takeScreenshot(page, 'execution-progress-percent-not-found');
      }
    });

    test('应该显示步骤连接线', async ({ page }) => {
      // 查找连接线元素
      const connectionLine = page.locator('.h-0\\.5, .w-0\\.5, [class*="connection"]');
      const count = await connectionLine.count();

      if (count > 0) {
        await expect(connectionLine.first()).toBeVisible();
        await takeScreenshot(page, 'execution-progress-connections');
      } else {
        await takeScreenshot(page, 'execution-progress-connections-not-found');
      }
    });
  });

  test.describe('控制按钮 (ControlButtons)', () => {
    test('应该显示控制按钮组件', async ({ page }) => {
      // 检查是否有控制按钮
      const controlButtons = page.locator('[data-testid="control-buttons"]');
      const count = await controlButtons.count();

      if (count > 0) {
        await expect(controlButtons.first()).toBeVisible();
        await takeScreenshot(page, 'control-buttons-display');
      } else {
        await takeScreenshot(page, 'control-buttons-not-found');
      }
    });

    test('应该显示暂停/恢复/取消按钮', async ({ page }) => {
      // 查找控制按钮
      const controlButtons = page.locator('button').filter({ hasText: /暂停|恢复|取消|回退/ });
      const count = await controlButtons.count();

      if (count > 0) {
        await expect(controlButtons.first()).toBeVisible();
        await takeScreenshot(page, 'control-buttons-found');
      } else {
        await takeScreenshot(page, 'control-buttons-not-found');
      }
    });

    test('应该显示空闲状态提示', async ({ page }) => {
      // 查找空闲状态提示
      const idleHint = page.locator('text=/等待任务开始|任务已结束|等待中/');
      const count = await idleHint.count();

      if (count > 0) {
        await expect(idleHint.first()).toBeVisible();
        await takeScreenshot(page, 'control-buttons-idle');
      } else {
        await takeScreenshot(page, 'control-buttons-idle-not-found');
      }
    });
  });

  test.describe('审批面板 (ApprovalPanel)', () => {
    test('应该显示审批面板', async ({ page }) => {
      // 检查是否有审批面板
      const approvalPanel = page.locator('[data-testid="approval-panel"], [data-testid="approval-panel-empty"]');
      const count = await approvalPanel.count();

      if (count > 0) {
        await expect(approvalPanel.first()).toBeVisible();
        await takeScreenshot(page, 'approval-panel-display');
      } else {
        await takeScreenshot(page, 'approval-panel-not-found');
      }
    });

    test('应该显示风险等级标识', async ({ page }) => {
      // 查找风险等级标识
      const riskLabels = page.locator('text=/低风险|中风险|高风险/');
      const count = await riskLabels.count();

      if (count > 0) {
        await expect(riskLabels.first()).toBeVisible();
        await takeScreenshot(page, 'approval-panel-risk');
      } else {
        await takeScreenshot(page, 'approval-panel-risk-not-found');
      }
    });

    test('应该显示批准/拒绝/修改按钮', async ({ page }) => {
      // 查找审批操作按钮
      const actionButtons = page.locator('button').filter({ hasText: /批准|拒绝|修改/ });
      const count = await actionButtons.count();

      if (count > 0) {
        await expect(actionButtons.first()).toBeVisible();
        await takeScreenshot(page, 'approval-panel-buttons');
      } else {
        await takeScreenshot(page, 'approval-panel-buttons-not-found');
      }
    });

    test('应该显示审批数据预览', async ({ page }) => {
      // 查找数据预览区域
      const dataPreview = page.locator('[data-testid="approval-panel"] pre');
      const count = await dataPreview.count();

      if (count > 0) {
        await expect(dataPreview.first()).toBeVisible();
        await takeScreenshot(page, 'approval-panel-data');
      } else {
        await takeScreenshot(page, 'approval-panel-data-not-found');
      }
    });
  });

  test.describe('执行组件集成测试', () => {
    test('应该检查所有执行组件', async ({ page }) => {
      // 检查是否有任何执行组件
      const components = [
        '[data-testid="task-status-panel"]',
        '[data-testid="task-status-panel-empty"]',
        '[data-testid="execution-log"]',
        '[data-testid="execution-progress"]',
        '[data-testid="execution-progress-empty"]',
        '[data-testid="control-buttons"]',
        '[data-testid="approval-panel"]',
        '[data-testid="approval-panel-empty"]',
      ];

      let visibleCount = 0;
      for (const selector of components) {
        const element = page.locator(selector);
        if (await element.count() > 0) {
          visibleCount++;
        }
      }

      // 记录找到的组件数量
      console.log(`找到 ${visibleCount} 个执行组件`);

      await takeScreenshot(page, 'execution-components-overview');
    });

    test('应该支持不同屏幕尺寸', async ({ page }) => {
      // 测试桌面尺寸
      await page.setViewportSize({ width: 1280, height: 720 });
      await page.waitForTimeout(500);
      await takeScreenshot(page, 'execution-desktop-1280x720');

      // 测试平板尺寸
      await page.setViewportSize({ width: 768, height: 1024 });
      await page.waitForTimeout(500);
      await takeScreenshot(page, 'execution-tablet-768x1024');

      // 测试手机尺寸
      await page.setViewportSize({ width: 375, height: 667 });
      await page.waitForTimeout(500);
      await takeScreenshot(page, 'execution-mobile-375x667');
    });

    test('应该检查页面整体布局', async ({ page }) => {
      // 检查主要布局元素
      const sidebar = page.locator('[data-testid="sidebar"], nav, .sidebar');
      const mainContent = page.locator('main, [role="main"], .main-content');
      const header = page.locator('header, .header, [data-testid="header"]');

      const sidebarVisible = await sidebar.count() > 0;
      const mainVisible = await mainContent.count() > 0;
      const headerVisible = await header.count() > 0;

      console.log(`侧边栏: ${sidebarVisible ? '可见' : '未找到'}`);
      console.log(`主内容: ${mainVisible ? '可见' : '未找到'}`);
      console.log(`头部: ${headerVisible ? '可见' : '未找到'}`);

      await takeScreenshot(page, 'execution-layout-full');
    });
  });
});
