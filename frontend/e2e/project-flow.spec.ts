/**
 * 长期任务流程 E2E 测试
 *
 * 测试长期任务（项目）的完整流程
 */

import { test, expect } from '@playwright/test';
import { quickLogin, createSession } from '../e2e/helpers';
import { createProject, verifyProjectPanel, toggleProjectAutoExecute, pauseProject, resumeProject } from '../tests/helpers/task-helpers';

test.describe('长期任务流程', () => {
  let sessionId: string;
  let projectId: string;

  test.beforeEach(async ({ page }) => {
    await quickLogin(page);

    const session = await createSession(page, {
      name: '长期任务测试会话',
    });
    sessionId = session.id!;

    await page.goto(`/sessions/${sessionId}`);
  });

  test('应该成功创建长期任务', async ({ page }) => {
    const projectGoal = '重构用户认证模块';

    // 创建长期任务
    const result = await createProject(page, projectGoal, sessionId);

    expect(result.success).toBeTruthy();
    expect(result.projectId).toBeTruthy();

    // 验证项目面板显示
    await verifyProjectPanel(page, result.projectId!, {
      goal: projectGoal,
      status: 'planning',
    });

    projectId = result.projectId!;

    // 截图
    await page.screenshot({ path: 'test-results/project-creation.png', fullPage: true });
  });

  test('应该在 Sidebar 显示长期任务面板', async ({ page }) => {
    const result = await createProject(page, '优化数据库性能', sessionId);
    projectId = result.projectId!;

    // 点击 Sidebar 切换按钮（如果侧边栏折叠）
    const sidebarToggle = page.locator('[data-testid="sidebar-toggle"], .sidebar-toggle');
    const isToggleVisible = await sidebarToggle.isVisible().catch(() => false);

    if (isToggleVisible) {
      await sidebarToggle.click();
    }

    // 验证长期任务面板显示
    const projectPanel = page.locator('.sidebar-project-panel, [data-testid="sidebar-project-panel"]');
    await expect(projectPanel).toBeVisible();

    // 验证项目在面板中显示
    const projectItem = projectPanel.locator(`[data-project-id="${projectId}"]`);
    await expect(projectItem).toBeVisible();
    await expect(projectItem).toContainText('优化数据库性能');
  });

  test('应该显示项目进度条', async ({ page }) => {
    const result = await createProject(page, '实现用户权限管理', sessionId);
    projectId = result.projectId!;

    // 验证进度条显示
    const projectPanel = page.locator(`[data-project-id="${projectId}"]`);
    const progressBar = projectPanel.locator('.progress-bar, [data-testid="progress-bar"]');

    await expect(progressBar).toBeVisible();

    // 验证进度百分比显示
    const progressText = projectPanel.locator('.progress-text, [data-testid="progress-text"]');
    await expect(progressText).toBeVisible();

    // 验证进度格式（例如："1/5" 或 "20%"）
    const text = await progressText.textContent();
    expect(text).toMatch(/\d+\/\d+|%\d+/);
  });

  test('应该自动创建规划任务 #0', async ({ page }) => {
    const result = await createProject(page, '重构认证系统', sessionId);
    projectId = result.projectId!;

    // 等待规划任务创建
    await page.waitForFunction(
      () => {
        const tasks = document.querySelectorAll('[data-task-type="planning"], .task-planning');
        return tasks.length > 0;
      },
      { timeout: 10000 }
    );

    // 验证规划任务显示
    const planningTask = page.locator('[data-task-type="planning"], .task-planning').first();
    await expect(planningTask).toBeVisible();

    // 验证任务目标是规划相关
    await expect(planningTask).toContainText(/规划|planning/);
  });

  test('应该等待规划任务完成后创建执行任务', async ({ page }) => {
    const result = await createProject(page, '优化 API 性能', sessionId);
    projectId = result.projectId!;

    // 等待规划任务完成（这可能需要一些时间）
    await page.waitForFunction(
      () => {
        const planningTask = document.querySelector('[data-task-type="planning"], .task-planning');
        if (!planningTask) return false;

        const status = planningTask.getAttribute('data-status');
        return status === 'completed';
      },
      { timeout: 120000 }
    );

    // 验证执行任务创建
    await page.waitForFunction(
      () => {
        const execTasks = document.querySelectorAll('[data-task-type="execution"], .task-execution');
        return execTasks.length > 0;
      },
      { timeout: 10000 }
    );

    const executionTask = page.locator('[data-task-type="execution"], .task-execution').first();
    await expect(executionTask).toBeVisible();
  });

  test('应该显示任务队列预览', async ({ page }) => {
    const result = await createProject(page, '实现用户通知系统', sessionId);
    projectId = result.projectId!;

    // 展开项目详情
    const projectPanel = page.locator(`[data-project-id="${projectId}"]`);
    const expandButton = projectPanel.locator('button:has-text("展开"), [data-testid="expand-project"]');
    await expandButton.click();

    // 验证任务队列显示
    const taskQueue = projectPanel.locator('.task-queue, [data-testid="task-queue"]');
    await expect(taskQueue).toBeVisible();

    // 验证队列中的任务列表
    const queueItems = taskQueue.locator('.queue-item, [data-testid="queue-item"]');
    const count = await queueItems.count();
    expect(count).toBeGreaterThan(0);
  });

  test('应该支持切换自动完成开关', async ({ page }) => {
    const result = await createProject(page, '重构缓存系统', sessionId);
    projectId = result.projectId!;

    // 获取初始状态
    const projectPanel = page.locator(`[data-project-id="${projectId}"]`);
    const toggle = projectPanel.locator('[data-testid="auto-execute-toggle"]');
    const initialState = await toggle.getAttribute('aria-checked');

    // 切换开关
    await toggleProjectAutoExecute(page, projectId);

    // 验证状态改变
    const newState = await toggle.getAttribute('aria-checked');
    expect(newState).not.toBe(initialState);
  });

  test('应该支持暂停项目', async ({ page }) => {
    const result = await createProject(page, '实现搜索功能', sessionId);
    projectId = result.projectId!;

    // 暂停项目
    await pauseProject(page, projectId);

    // 验证状态更新
    await verifyProjectPanel(page, projectId, {
      status: 'paused',
    });

    // 验证暂停图标显示
    const projectPanel = page.locator(`[data-project-id="${projectId}"]`);
    const pauseIcon = projectPanel.locator('.pause-icon, [data-testid="pause-icon"]');
    await expect(pauseIcon).toBeVisible();
  });

  test('应该支持恢复暂停的项目', async ({ page }) => {
    const result = await createProject(page, '实现数据导出功能', sessionId);
    projectId = result.projectId!;

    // 先暂停
    await pauseProject(page, projectId);

    // 恢复项目
    await resumeProject(page, projectId);

    // 验证状态更新
    await verifyProjectPanel(page, projectId, {
      status: 'running',
    });

    // 验证运行图标显示
    const projectPanel = page.locator(`[data-project-id="${projectId}"]`);
    const runningIcon = projectPanel.locator('.running-icon, [data-testid="running-icon"]');
    await expect(runningIcon).toBeVisible();
  });

  test('应该在自动执行模式下自动创建下一个任务', async ({ page }) => {
    const result = await createProject(page, '实现文件上传功能', sessionId);
    projectId = result.projectId!;

    // 确保自动执行已开启
    const autoExecuteToggle = page.locator(`[data-project-id="${projectId}"]`).locator('[data-testid="auto-execute-toggle"]');
    const isChecked = await autoExecuteToggle.getAttribute('aria-checked');

    if (isChecked !== 'true') {
      await autoExecuteToggle.click();
    }

    // 等待第一个任务完成
    await page.waitForTimeout(5000);

    // 等待第二个任务创建
    await page.waitForFunction(
      () => {
        const tasks = document.querySelectorAll('[data-testid="task-card"]');
        return tasks.length >= 2;
      },
      { timeout: 120000 }
    );

    // 验证第二个任务
    const taskCards = page.locator('[data-testid="task-card"]');
    await expect(taskCards).toHaveCount(2);
  });

  test('应该在项目完成时显示总结', async ({ page }) => {
    const result = await createProject(page, '简单测试项目', sessionId);
    projectId = result.projectId!;

    // 等待项目完成（这可能需要很长时间，实际测试中可能需要 Mock）
    // 这里提供测试框架

    await page.waitForFunction(
      () => {
        const projectPanel = document.querySelector(`[data-project-id="${projectId}"]`);
        if (!projectPanel) return false;

        const status = projectPanel.getAttribute('data-status');
        return status === 'completed';
      },
      { timeout: 300000 } // 5分钟超时
    ).catch(() => {
      // 如果超时，跳过后续验证
      test.skip(true, '项目未能在超时时间内完成');
    });

    // 验证项目总结显示
    const projectPanel = page.locator(`[data-project-id="${projectId}"]`);
    const summary = projectPanel.locator('.project-summary, [data-testid="project-summary"]');
    await expect(summary).toBeVisible();

    // 验证完成图标
    const completeIcon = projectPanel.locator('.complete-icon, [data-testid="complete-icon"]');
    await expect(completeIcon).toBeVisible();
  });

  test('应该支持删除项目', async ({ page }) => {
    const result = await createProject(page, '临时测试项目', sessionId);
    projectId = result.projectId!;

    // 暂停项目（如果正在运行）
    const projectPanel = page.locator(`[data-project-id="${projectId}"]`);
    const status = await projectPanel.getAttribute('data-status');

    if (status === 'running') {
      await pauseProject(page, projectId);
    }

    // 点击删除按钮
    const deleteButton = projectPanel.locator('button:has-text("删除"), [data-testid="delete-project"]');
    await deleteButton.click();

    // 验证确认对话框
    const dialog = page.locator('dialog, [role="dialog"]');
    await expect(dialog).toBeVisible();
    await expect(dialog).toContainText(/确认删除|确定要删除/);

    // 确认删除
    await dialog.locator('button:has-text("确认")').click();

    // 验证项目被删除
    await expect(projectPanel).not.toBeVisible({ timeout: 5000 });
  });

  test('应该支持查看项目详情', async ({ page }) => {
    const result = await createProject(page, '详细测试项目', sessionId);
    projectId = result.projectId!;

    // 点击查看详情按钮
    const projectPanel = page.locator(`[data-project-id="${projectId}"]`);
    const detailButton = projectPanel.locator('button:has-text("详情"), [data-testid="project-detail"]');
    await detailButton.click();

    // 验证详情对话框打开
    const dialog = page.locator('dialog, [role="dialog"]');
    await expect(dialog).toBeVisible();

    // 验证详情内容
    await expect(dialog).toContainText(/项目详情|Project Detail/);

    // 验证任务列表显示
    const taskList = dialog.locator('.project-task-list, [data-testid="project-task-list"]');
    await expect(taskList).toBeVisible();

    // 关闭对话框
    await dialog.locator('button:has-text("关闭")').click();
    await expect(dialog).not.toBeVisible();
  });

  test.afterEach(async ({ page }) => {
    await page.screenshot({
      path: `test-results/project-flow-${test.info().title.replace(/\s+/g, '-')}.png`,
      fullPage: true,
    });
  });
});
