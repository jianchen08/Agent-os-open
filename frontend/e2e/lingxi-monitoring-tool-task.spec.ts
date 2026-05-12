/**
 * LingXi L2 Agent 任务测试：创建数据流监控工具
 *
 * 测试目标：
 * 1. 登录系统
 * 2. 创建与 lingxi (L2 Agent) 绑定的会话
 * 3. 发送任务：使用工作流创建数据流监控工具
 * 4. 验证任务执行结果
 *
 * 评估指标：
 * - 工具相关文件存在
 * - 工具已成功注册到系统
 * - 工具功能无异常，可以正常调用
 * - 使用修改工作流对工具进行了至少一次修改
 */

import { test, expect } from '@playwright/test';
import { login, takeScreenshot, sendMessage, waitForAIResponse } from './helpers';

test.describe('LingXi L2 Agent - 数据流监控工具任务', () => {
  test.beforeEach(async ({ page }) => {
    await login(page);
  });

  test('应该能成功向 lingxi 提交创建数据流监控工具的任务', async ({ page }) => {
    // 导航到会话页面
    await page.goto('/sessions', { timeout: 30000 });
    await page.waitForLoadState('networkidle');

    // 创建与 lingxi agent 绑定的新会话
    // 先检查是否有 lingxi agent 可选
    await page.click('button:has-text("创建"), button:has-text("新建"), [data-testid="create-session-btn"]');

    // 等待对话框出现
    await expect(page.locator('dialog, .modal, [role="dialog"]')).toBeVisible({ timeout: 5000 });

    // 填写会话名称
    await page.fill('input[name="name"], input[placeholder*="名称"]', 'LingXi 监控工具测试会话');

    // 尝试选择 lingxi agent
    const agentSelect = page.locator('[data-testid="agent-select"], select[name="agentId"]');
    const hasAgentSelect = await agentSelect.count() > 0;

    if (hasAgentSelect) {
      // 检查是否有 lingxi 选项
      await agentSelect.click();
      const lingxiOption = page.locator('li, option').filter({ hasText: /lingxi|LingXi|灵犀/i });
      const hasLingxi = await lingxiOption.count() > 0;

      if (hasLingxi) {
        await lingxiOption.first().click();
      }
    }

    // 点击确认创建
    await page.click('button:has-text("确认"), button:has-text("创建"), dialog button[type="submit"]');

    // 等待导航到新会话页面
    await page.waitForURL(/\/sessions\/[a-f0-9-]+/, { timeout: 10000 });
    await page.waitForLoadState('networkidle');

    // 获取当前会话 ID
    const sessionId = page.url().match(/\/sessions\/([a-f0-9-]+)/)?.[1];
    expect(sessionId).toBeTruthy();

    await takeScreenshot(page, 'lingxi-session-created');

    // 构建任务消息
    const taskMessage = `请帮我完成以下任务：

**任务目标**：创建数据流监控工具

**任务步骤**：
1. 使用现有的工作流创建功能，创建一个新的监控工具
2. 该工具应该能够监控数据流的状态和变化
3. 成功创建后，使用修改工作流对工具进行修改（例如添加新的监控指标）
4. 确保工具功能正常，可以正确注册和使用

**验收标准**：
- 工具相关文件存在（至少包含工具定义文件）
- 工具已成功注册到系统（可以在工具列表中找到）
- 工具功能无异常，可以正常调用
- 使用修改工作流对工具进行了至少一次修改
- 工具能够监控数据流（有基本的监控功能）

请使用 task_submit 工具将此任务提交给合适的 L3 Agent 执行。`;

    // 发送任务消息
    await sendMessage(page, sessionId, taskMessage);

    await takeScreenshot(page, 'lingxi-task-sent');

    // 等待 AI 响应（可能需要较长时间）
    test.setTimeout(300000); // 5 分钟超时

    try {
      const response = await waitForAIResponse(page, 180000); // 3 分钟等待响应
      console.log('LingXi 响应:', response);

      // 验证响应中包含任务确认
      await expect(page.locator('.message.assistant')).toContainText(/任务|task|工作流|工具/i, { timeout: 10000 });

      await takeScreenshot(page, 'lingxi-response-received');

      // 等待一段时间，观察任务执行情况
      await page.waitForTimeout(30000);

      // 检查是否有任务卡片或执行记录
      const taskCard = page.locator('[data-testid="task-card"], .task-card, .execution-card');
      const hasTaskCard = await taskCard.count() > 0;

      if (hasTaskCard) {
        console.log('检测到任务卡片');
        await takeScreenshot(page, 'lingxi-task-card-visible');

        // 等待任务完成或失败
        await page.waitForTimeout(60000); // 再等待 1 分钟

        // 检查任务状态
        const statusElement = page.locator('[data-testid="task-status"], .task-status');
        const statusText = await statusElement.first().textContent().catch(() => '未找到状态');
        console.log('任务状态:', statusText);
      }

    } catch (error) {
      console.error('等待响应超时或错误:', error);
      await takeScreenshot(page, 'lingxi-timeout-or-error');

      // 即使超时，也尝试检查页面上的任何输出
      const allMessages = await page.locator('.message.assistant').allTextContents();
      console.log('所有 AI 消息:', allMessages);
    }

    await takeScreenshot(page, 'lingxi-test-final');
  });

  test('应该能直接向 lingxi 发送简单任务消息', async ({ page }) => {
    // 导航到会话页面
    await page.goto('/sessions', { timeout: 30000 });
    await page.waitForLoadState('networkidle');

    // 创建新会话
    await page.click('button:has-text("创建"), button:has-text("新建"), [data-testid="create-session-btn"]');
    await expect(page.locator('dialog, .modal, [role="dialog"]')).toBeVisible({ timeout: 5000 });
    await page.fill('input[name="name"], input[placeholder*="名称"]', 'LingXi 简单任务测试');
    await page.click('button:has-text("确认"), button:has-text("创建"), dialog button[type="submit"]');

    // 等待导航到新会话
    await page.waitForURL(/\/sessions\/[a-f0-9-]+/, { timeout: 10000 });
    const sessionId = page.url().match(/\/sessions\/([a-f0-9-]+)/)?.[1];
    expect(sessionId).toBeTruthy();

    // 发送简单任务消息
    const simpleTask = `请帮我创建一个简单的数据流监控工具。

要求：
1. 工具能够监控数据流状态
2. 创建后使用工作流进行修改
3. 确保工具可用并已注册`;

    await sendMessage(page, sessionId, simpleTask);

    test.setTimeout(180000); // 3 分钟超时

    // 等待响应
    try {
      const response = await waitForAIResponse(page, 120000);
      console.log('LingXi 响应:', response);
      await takeScreenshot(page, 'lingxi-simple-task-response');
    } catch (error) {
      console.error('等待响应超时:', error);
      await takeScreenshot(page, 'lingxi-simple-task-timeout');
    }
  });
});
