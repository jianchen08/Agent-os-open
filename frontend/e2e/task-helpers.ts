/**
 * 任务执行闭环测试辅助工具
 *
 * 提供任务相关的测试辅助函数
 *
 * @docs docs/tasks/task-execution-loop-system.md
 */

import { Page, Locator } from '@playwright/test';

/**
 * 任务辅助类
 *
 * 封装任务相关的测试操作
 */
export class TaskHelpers {
  constructor(private page: Page) {}

  /**
   * 创建任务
   * @param goal 任务目标
   * @param waitForCompletion 是否等待任务完成
   * @returns 任务ID
   */
  async createTask(goal: string, waitForCompletion = false) {
    // 填写任务目标
    const chatInput = this.page.locator('textarea[placeholder*="消息"], [data-testid="chat-input"]');
    await chatInput.fill(goal);

    // 监听创建任务API
    const createTaskRequest = this.page.waitForRequest(
      (req) => req.url().includes('/api/tasks') && req.method() === 'POST',
      { timeout: 10000 }
    ).catch(() => null);

    // 点击发送按钮
    const sendButton = this.page.locator('button:has-text("发送"), [data-testid="send-button"]');
    await sendButton.click();

    // 等待任务卡片出现
    await this.page.waitForSelector('[data-testid="task-card"]', { timeout: 10000 });

    // 如果需要等待任务完成
    if (waitForCompletion) {
      await this.waitForTaskCompletion(goal);
    }

    // 返回任务ID（从URL或响应中获取）
    if (createTaskRequest) {
      const url = createTaskRequest.url();
      const taskId = await this.page.evaluate(async (requestUrl) => {
        const response = await fetch(requestUrl);
        const data = await response.json();
        return data.id || data.taskId;
      }, url);

      return taskId;
    }

    return null;
  }

  /**
   * 等待任务达到指定阶段
   * @param taskId 任务ID
   * @param phase 阶段名称 (prepare/execute/evaluate)
   * @param timeout 超时时间（毫秒）
   */
  async waitForTaskPhase(taskId: string, phase: 'prepare' | 'execute' | 'evaluate', timeout = 30000) {
    await this.page.waitForSelector(
      `[data-testid="task-card"][data-task-id="${taskId}"] [data-phase="${phase}"]`,
      { state: 'visible', timeout }
    );
  }

  /**
   * 等待任务完成
   * @param goal 任务目标（用于定位）
   * @param timeout 超时时间（毫秒）
   */
  async waitForTaskCompletion(goal: string, timeout = 60000) {
    const startTime = Date.now();

    while (Date.now() - startTime < timeout) {
      // 查找包含指定目标的任务卡片
      const taskCard = this.page.locator(`[data-testid="task-card"]`).filter({ hasText: goal });

      // 检查是否有完成状态
      const completedStatus = taskCard.locator('[data-status="completed"], [data-phase="evaluate"][data-phase-status="completed"]');
      const isCompleted = await completedStatus.count() > 0;

      if (isCompleted) {
        return;
      }

      // 等待一段时间再检查
      await this.page.waitForTimeout(2000);
    }

    throw new Error(`任务完成超时: ${goal}`);
  }

  /**
   * 获取任务卡片元素
   * @param taskId 任务ID
   * @returns 任务卡片定位器
   */
  getTaskCard(taskId: string): Locator {
    return this.page.locator(`[data-testid="task-card"][data-task-id="${taskId}"]`);
  }

  /**
   * 获取任务当前阶段
   * @param taskId 任务ID
   * @returns 当前阶段名称
   */
  async getTaskCurrentPhase(taskId: string): Promise<string | null> {
    const taskCard = this.getTaskCard(taskId);
    const currentPhase = taskCard.locator('[data-testid="current-phase"]');

    const phaseText = await currentPhase.textContent().catch(() => null);
    return phaseText;
  }

  /**
   * 点击Agent Tab
   * @param agentName Agent名称
   */
  async clickAgentTab(agentName: string) {
    const tab = this.page.locator(`[data-testid="agent-tab"][data-agent-name="${agentName}"]`);
    await tab.click();
  }

  /**
   * 点击Agent Tab（通过索引）
   * @param index Tab索引（从0开始）
   */
  async clickAgentTabByIndex(index: number) {
    const tabs = this.page.locator('[data-testid="agent-tab"]');
    await tabs.nth(index).click();
  }

  /**
   * 获取当前活跃的Agent Tab
   * @returns Agent Tab定位器
   */
  getActiveAgentTab(): Locator {
    return this.page.locator('[data-testid="agent-tab"][data-active="true"], [data-testid="agent-tab"].active');
  }

  /**
   * 切换面板状态
   * @param panelType 面板类型 (execution-graph/task-panel)
   */
  async togglePanel(panelType: 'execution-graph' | 'task-panel') {
    const button = this.page.locator(`[data-testid="toggle-${panelType}-button"]`);
    await button.click();
  }

  /**
   * 验证任务状态
   * @param taskId 任务ID
   * @param expectedStatus 期望状态
   */
  async verifyTaskStatus(taskId: string, expectedStatus: 'pending' | 'running' | 'completed' | 'failed') {
    const taskCard = this.getTaskCard(taskId);
    const statusIndicator = taskCard.locator(`[data-status="${expectedStatus}"]`);
    await statusIndicator.waitFor({ state: 'visible', timeout: 5000 });
  }

  /**
   * 验证任务阶段
   * @param taskId 任务ID
   * @param expectedPhase 期望阶段
   */
  async verifyTaskPhase(taskId: string, expectedPhase: 'prepare' | 'execute' | 'evaluate') {
    const taskCard = this.getTaskCard(taskId);
    const phaseIndicator = taskCard.locator(`[data-phase="${expectedPhase}"]`);
    await expect(phaseIndicator).toBeVisible();
  }

  /**
   * 获取任务验收标准列表
   * @param taskId 任务ID
   * @returns 验收标准数组
   */
  async getTaskAcceptanceCriteria(taskId: string): Promise<Array<{ description: string; status: string }>> {
    const taskCard = this.getTaskCard(taskId);

    // 展开详情
    const expandButton = taskCard.locator('button[aria-label*="展开"]');
    const expandVisible = await expandButton.isVisible().catch(() => false);
    if (expandVisible) {
      await expandButton.click();
    }

    // 展开AC列表
    const acListButton = taskCard.locator('button:has-text("展开")');
    const acListVisible = await acListButton.isVisible().catch(() => false);
    if (acListVisible) {
      await acListButton.click();
    }

    // 获取所有AC项目
    const acItems = taskCard.locator('[data-testid="ac-item"]');
    const count = await acItems.count();

    const criteria: Array<{ description: string; status: string }> = [];

    for (let i = 0; i < count; i++) {
      const item = acItems.nth(i);
      const description = await item.locator('[data-testid="ac-description"]').textContent();
      const status = await item.locator('[data-testid="ac-status"]').getAttribute('data-status');

      criteria.push({
        description: description?.trim() || '',
        status: status || 'unknown',
      });
    }

    return criteria;
  }

  /**
   * 验证验收标准状态
   * @param taskId 任务ID
   * @param acIndex AC索引
   * @param expectedStatus 期望状态
   */
  async verifyACStatus(taskId: string, acIndex: number, expectedStatus: 'pending' | 'passed' | 'failed') {
    const taskCard = this.getTaskCard(taskId);
    const acItem = taskCard.locator('[data-testid="ac-item"]').nth(acIndex);
    const status = acItem.locator(`[data-status="${expectedStatus}"]`);
    await expect(status).toBeVisible();
  }

  /**
   * 创建长期任务（项目）
   * @param goal 长期目标
   * @param autoExecute 是否自动执行
   * @returns 项目ID
   */
  async createProject(goal: string, autoExecute = false) {
    // 填写长期目标
    const chatInput = this.page.locator('textarea[placeholder*="消息"], [data-testid="chat-input"]');
    await chatInput.fill(`创建长期任务：${goal}`);

    // 监听创建项目API
    const createProjectRequest = this.page.waitForRequest(
      (req) => req.url().includes('/api/projects') && req.method() === 'POST',
      { timeout: 10000 }
    ).catch(() => null);

    // 点击发送按钮
    const sendButton = this.page.locator('button:has-text("发送"), [data-testid="send-button"]');
    await sendButton.click();

    // 等待项目卡片出现
    await this.page.waitForSelector('[data-testid="project-card"], [data-testid="task-card"]', { timeout: 10000 });

    // 返回项目ID
    if (createProjectRequest) {
      const url = createProjectRequest.url();
      const projectId = await this.page.evaluate(async (requestUrl) => {
        const response = await fetch(requestUrl);
        const data = await response.json();
        return data.id || data.projectId;
      }, url);

      return projectId;
    }

    return null;
  }

  /**
   * 切换项目自动完成开关
   * @param projectId 项目ID
   * @param enabled 是否启用
   */
  async toggleProjectAutoExecute(projectId: string, enabled: boolean) {
    const toggle = this.page.locator(`[data-testid="project-card"][data-project-id="${projectId}"] [data-testid="auto-execute-toggle"]`);
    await toggle.click();

    // 等待状态更新
    await this.page.waitForTimeout(500);

    // 验证状态
    const currentState = await toggle.getAttribute('data-state');
    const expectedState = enabled ? 'checked' : 'unchecked';

    if (currentState !== expectedState) {
      throw new Error(`自动完成开关状态不正确：期望 ${expectedState}，实际 ${currentState}`);
    }
  }

  /**
   * 暂停项目
   * @param projectId 项目ID
   */
  async pauseProject(projectId: string) {
    const pauseButton = this.page.locator(`[data-testid="project-card"][data-project-id="${projectId}"] button:has-text("暂停")`);
    await pauseButton.click();

    // 等待状态更新
    await this.page.waitForTimeout(500);

    // 验证状态
    const status = this.page.locator(`[data-testid="project-card"][data-project-id="${projectId}"] [data-testid="project-status"]`);
    await expect(status).toContainText('已暂停', { timeout: 5000 });
  }

  /**
   * 恢复项目
   * @param projectId 项目ID
   */
  async resumeProject(projectId: string) {
    const resumeButton = this.page.locator(`[data-testid="project-card"][data-project-id="${projectId}"] button:has-text("恢复")`);
    await resumeButton.click();

    // 等待状态更新
    await this.page.waitForTimeout(500);

    // 验证状态
    const status = this.page.locator(`[data-testid="project-card"][data-project-id="${projectId}"] [data-testid="project-status"]`);
    await expect(status).toContainText('运行中', { timeout: 5000 });
  }

  /**
   * 获取项目进度
   * @param projectId 项目ID
   * @returns 当前进度和总数
   */
  async getProjectProgress(projectId: string): Promise<{ current: number; total: number }> {
    const projectCard = this.page.locator(`[data-testid="project-card"][data-project-id="${projectId}"]`);
    const progressText = await projectCard.locator('text=/\\d+\\/\\d+/').textContent();

    const match = progressText?.match(/(\d+)\/(\d+)/);
    if (match) {
      return {
        current: parseInt(match[1], 10),
        total: parseInt(match[2], 10),
      };
    }

    return { current: 0, total: 0 };
  }

  /**
   * 等待WebSocket连接
   * @param timeout 超时时间（毫秒）
   */
  async waitForWebSocketConnection(timeout = 10000) {
    await this.page.waitForSelector(
      '[data-testid="websocket-status"][data-status="connected"], .h-2.w-2.rounded-full.bg-green-500',
      { timeout }
    );
  }

  /**
   * 获取WebSocket状态
   * @returns 连接状态
   */
  async getWebSocketStatus(): Promise<string> {
    const statusIndicator = this.page.locator('[data-testid="websocket-status"]');
    const status = await statusIndicator.getAttribute('data-status');
    return status || 'unknown';
  }

  /**
   * 等待任务阶段变化事件
   * @param taskId 任务ID
   * @param expectedPhase 期望阶段
   * @param timeout 超时时间（毫秒）
   */
  async waitForTaskPhaseChangeEvent(taskId: string, expectedPhase: string, timeout = 30000) {
    const startTime = Date.now();

    while (Date.now() - startTime < timeout) {
      const currentPhase = await this.getTaskCurrentPhase(taskId);

      if (currentPhase === expectedPhase) {
        return;
      }

      await this.page.waitForTimeout(1000);
    }

    throw new Error(`等待任务阶段变化超时：期望 ${expectedPhase}`);
  }

  /**
   * 截图并保存（带任务相关前缀）
   * @param name 截图名称
   */
  async screenshot(name: string) {
    await this.page.screenshot({
      path: `test-results/task-tests/${name}.png`,
      fullPage: true,
    });
  }
}

/**
 * 创建任务辅助对象
 * @param page Playwright Page对象
 * @returns TaskHelpers实例
 */
export function createTaskHelpers(page: Page): TaskHelpers {
  return new TaskHelpers(page);
}
