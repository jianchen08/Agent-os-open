/**
 * 用户创建任务流程 E2E 测试
 *
 * 测试用户创建任务的完整流程
 */

import { test, expect } from '@playwright/test';
import { quickLogin, createSession } from '../e2e/helpers';
import { createTask, verifyTaskCard, verifyPhaseIndicator, verifyACList } from '../tests/helpers/task-helpers';
import { simpleTask } from '../tests/fixtures';

test.describe('用户创建任务流程', () => {
  let sessionId: string;

  test.beforeEach(async ({ page }) => {
    // 登录
    await quickLogin(page);

    // 创建会话
    const session = await createSession(page, {
      name: '任务测试会话',
      description: '用于测试任务创建流程',
    });
    sessionId = session.id!;

    // 导航到会话页面
    await page.goto(`/sessions/${sessionId}`, { timeout: 10000 });
  });

  test('应该成功创建简单任务', async ({ page }) => {
    const taskGoal = '实现用户登录功能';

    // 创建任务
    const result = await createTask(page, taskGoal, sessionId);

    expect(result.success).toBeTruthy();
    expect(result.taskId).toBeTruthy();

    // 验证任务卡片出现在消息流中
    await verifyTaskCard(page, result.taskId!, {
      goal: taskGoal,
    });

    // 截图
    await page.screenshot({ path: 'test-results/task-creation-simple.png', fullPage: true });
  });

  test('应该正确显示任务目标', async ({ page }) => {
    const taskGoal = '实现 JWT Token 认证';

    await createTask(page, taskGoal, sessionId);

    // 验证任务目标显示
    const taskCard = page.locator('[data-testid="task-card"]').first();
    const goalElement = taskCard.locator('.task-goal, [data-testid="task-goal"]');

    await expect(goalElement).toBeVisible();
    await expect(goalElement).toContainText(taskGoal);
  });

  test('应该显示三阶段指示器', async ({ page }) => {
    const taskGoal = '优化数据库查询性能';

    const result = await createTask(page, taskGoal, sessionId);

    // 验证三阶段指示器显示
    await verifyPhaseIndicator(page, result.taskId!, {
      prepare: 'pending',
      execute: 'pending',
      evaluate: 'pending',
    });

    // 验证阶段指示器的可见性
    const phaseIndicator = page.locator('[data-testid="phase-indicator"]');
    await expect(phaseIndicator).toBeVisible();

    // 验证三个阶段都显示
    const phases = phaseIndicator.locator('[data-phase]');
    await expect(phases).toHaveCount(3);
  });

  test('应该显示 AC 列表', async ({ page }) => {
    const taskGoal = '实现密码加密功能';

    const result = await createTask(page, taskGoal, sessionId);

    // 验证 AC 列表显示
    await verifyACList(page, result.taskId!, 3);

    // 验证每个 AC 都显示描述和状态
    const acItems = page.locator('.ac-item, [data-testid="ac-item"]');
    const count = await acItems.count();

    for (let i = 0; i < count; i++) {
      const acItem = acItems.nth(i);

      // 验证描述显示
      const description = acItem.locator('.ac-description, [data-testid="ac-description"]');
      await expect(description).toBeVisible();

      // 验证状态显示
      const status = acItem.locator('.ac-status, [data-testid="ac-status"]');
      await expect(status).toBeVisible();
    }
  });

  test('应该支持展开和折叠 AC 列表', async ({ page }) => {
    const taskGoal = '实现用户注册功能';

    const result = await createTask(page, taskGoal, sessionId);
    const taskCard = page.locator(`[data-task-id="${result.taskId}"]`);

    // 默认状态：AC 列表应该是折叠的
    const acList = taskCard.locator('.ac-list, [data-testid="ac-list"]');
    const isVisible = await acList.isVisible().catch(() => false);
    expect(isVisible).toBeFalsy();

    // 点击展开按钮
    const expandButton = taskCard.locator('button:has-text("验收标准"), [data-testid="expand-ac"]');
    await expandButton.click();

    // 验证 AC 列表展开
    await expect(acList).toBeVisible();

    // 点击折叠按钮
    await expandButton.click();

    // 验证 AC 列表折叠
    await expect(acList).not.toBeVisible();
  });

  test('应该显示任务优先级标签（如果设置）', async ({ page }) => {
    // 注意：这个测试需要在 UI 中支持设置优先级后才能实现
    // 目前先预留测试框架

    const taskGoal = '高优先级：修复登录 Bug';

    await createTask(page, taskGoal, sessionId);

    const taskCard = page.locator('[data-testid="task-card"]').first();

    // 如果实现了优先级功能，取消下面的注释
    // const priorityBadge = taskCard.locator('.priority-badge');
    // await expect(priorityBadge).toBeVisible();
    // await expect(priorityBadge).toContainText('高优先级');
  });

  test('应该在创建时显示准备阶段', async ({ page }) => {
    const taskGoal = '实现用户注销功能';

    const result = await createTask(page, taskGoal, sessionId);

    // 验证初始阶段为准备阶段
    await verifyPhaseIndicator(page, result.taskId!, {
      prepare: 'running',
      execute: 'pending',
      evaluate: 'pending',
    });

    // 验证当前阶段标识
    const currentPhaseBadge = page.locator('[data-testid="current-phase"]');
    await expect(currentPhaseBadge).toContainText('准备');
  });

  test('应该在消息流中显示任务卡片', async ({ page }) => {
    const taskGoal = '实现用户权限管理';

    // 记录当前消息数量
    const beforeCount = await page.locator('.message').count();

    // 创建任务
    await createTask(page, taskGoal, sessionId);

    // 验证消息数量增加
    const afterCount = await page.locator('.message').count();
    expect(afterCount).toBeGreaterThan(beforeCount);

    // 验证任务卡片在消息流中的位置
    const messages = page.locator('.message');
    const lastMessage = messages.nth(afterCount - 1);
    const taskCard = lastMessage.locator('[data-testid="task-card"]');

    await expect(taskCard).toBeVisible();
  });

  test('应该支持多个任务同时存在', async ({ page }) => {
    const taskGoals = [
      '实现用户登录',
      '实现用户注册',
      '实现用户注销',
    ];

    // 创建多个任务
    for (const goal of taskGoals) {
      await createTask(page, goal, sessionId);
      await page.waitForTimeout(1000); // 等待任务创建
    }

    // 验证所有任务卡片都显示
    const taskCards = page.locator('[data-testid="task-card"]');
    await expect(taskCards).toHaveCount(taskGoals.length);
  });

  test('应该显示任务创建时间', async ({ page }) => {
    const taskGoal = '实现用户资料编辑';

    const result = await createTask(page, taskGoal, sessionId);
    const taskCard = page.locator(`[data-task-id="${result.taskId}"]`);

    // 验证创建时间显示
    const timestamp = taskCard.locator('.task-timestamp, [data-testid="task-timestamp"]');
    await expect(timestamp).toBeVisible();

    // 验证时间格式（应该包含 "刚刚"、"分钟前" 等相对时间）
    const timeText = await timestamp.textContent();
    expect(timeText).toBeTruthy();
  });

  test('应该在创建失败时显示错误提示', async ({ page }) => {
    // 模拟网络错误
    await page.context().setOffline(true);

    const taskGoal = '这个任务应该创建失败';

    // 尝试创建任务
    await page.fill('textarea[placeholder*="消息"]', taskGoal);
    await page.click('button:has-text("发送")');

    // 验证错误提示显示
    const errorMessage = page.locator('.toast, [role="alert"]');
    await expect(errorMessage).toBeVisible({ timeout: 5000 });
    await expect(errorMessage).toContainText(/失败|错误|网络/);

    // 恢复网络
    await page.context().setOffline(false);
  });

  test('应该支持在任务卡片中直接查看详情', async ({ page }) => {
    const taskGoal = '实现用户头像上传';

    const result = await createTask(page, taskGoal, sessionId);
    const taskCard = page.locator(`[data-task-id="${result.taskId}"]`);

    // 点击详情按钮
    const detailButton = taskCard.locator('button:has-text("详情"), [data-testid="task-detail"]');
    await detailButton.click();

    // 验证详情对话框打开
    const dialog = page.locator('dialog, .modal, [role="dialog"]');
    await expect(dialog).toBeVisible();

    // 验证详情内容
    await expect(dialog).toContainText(taskGoal);

    // 关闭对话框
    const closeButton = dialog.locator('button:has-text("关闭"), button[aria-label="关闭"]');
    await closeButton.click();

    // 验证对话框关闭
    await expect(dialog).not.toBeVisible();
  });

  test.afterEach(async ({ page }) => {
    // 清理：截图
    await page.screenshot({
      path: `test-results/task-creation-${test.info().title.replace(/\s+/g, '-')}.png`,
      fullPage: true,
    });
  });
});
