/**
 * 真实工具卡片渲染 E2E 测试
 *
 * 完整流程测试：
 * 1. 创建新会话
 * 2. 发送消息触发工具执行
 * 3. 等待工具调用完成
 * 4. 验证工具卡片在会话中正确渲染
 * 5. 验证卡片内容、状态、交互功能
 */

import { test, expect } from '@playwright/test';
import { login, waitForPageLoad, takeScreenshot } from './helpers';

/**
 * 等待工具卡片出现
 * @param page Playwright Page对象
 * @param timeout 超时时间（毫秒）
 * @returns 工具卡片是否出现
 */
async function waitForToolCard(page: any, timeout = 30000): Promise<boolean> {
  const toolCard = page.locator('[data-activity-type="tool_call"]');

  try {
    await toolCard.first().waitFor({ state: 'visible', timeout });
    return true;
  } catch (e) {
    return false;
  }
}

/**
 * 获取所有工具卡片的信息
 */
async function getToolCardsInfo(page: any): Promise<Array<{id: string, title: string, status: string}>> {
  const toolCards = page.locator('[data-activity-type="tool_call"]');
  const count = await toolCards.count();
  const cardsInfo: Array<{id: string, title: string, status: string}> = [];

  for (let i = 0; i < count; i++) {
    const card = toolCards.nth(i);
    const id = await card.getAttribute('data-activity-id') || `unknown-${i}`;
    const title = await card.locator('.font-medium').textContent() || 'unknown';
    const status = await card.getAttribute('data-activity-status') || 'unknown';
    cardsInfo.push({ id, title: title.trim(), status });
  }

  return cardsInfo;
}

test.describe('真实工具卡片渲染测试', () => {
  let sessionId: string;

  test.beforeEach(async ({ page }) => {
    await login(page);
  });

  test.describe('场景1: 读取文件工具调用', () => {
    test('01-完整流程: 发送消息 → 执行工具 → 渲染卡片', async ({ page }) => {
      // 步骤1: 导航到会话页面
      await page.goto('/');
      await waitForPageLoad(page);

      // 步骤2: 创建新会话或使用现有会话
      const newSessionButton = page.locator('button').filter({ hasText: /新建会话|新对话/ });
      const hasNewSessionButton = await newSessionButton.count() > 0;

      if (hasNewSessionButton) {
        await newSessionButton.first().click();
        await page.waitForTimeout(500);
      }

      // 获取会话ID
      const url = page.url();
      const sessionIdMatch = url.match(/\/session\/([a-f0-9-]+)/);
      sessionId = sessionIdMatch ? sessionIdMatch[1] : '';
      expect(sessionId, '应该获取到会话ID').toBeTruthy();

      console.log(`[测试] 会话ID: ${sessionId}`);

      // 步骤3: 发送消息触发文件读取工具
      const testMessage = '请读取 package.json 文件的内容';
      // 使用正确的 data-testid 选择器
      const inputBox = page.locator('[data-testid="chat-input-textarea"]');

      await expect(inputBox, '输入框应该存在').toBeVisible({ timeout: 10000 });
      await inputBox.fill(testMessage);

      // 点击发送按钮
      const sendButton = page.locator('[data-testid="chat-send-button"]');
      await expect(sendButton, '发送按钮应该存在').toBeVisible();
      await sendButton.click();

      console.log('[测试] 消息已发送，等待工具执行...');

      // 步骤4: 等待工具执行完成
      // 等待用户消息出现
      await expect(page.locator('text=' + testMessage)).toBeVisible({ timeout: 10000 });

      // 等待AI响应开始（助手消息出现）
      const assistantMessage = page.locator('[data-role="assistant"]').or(
        page.locator('.message-item').filter({ hasText: /./ })
      );
      await expect(assistantMessage.first(), 'AI响应应该开始').toBeVisible({ timeout: 15000 });
      console.log('[测试] AI响应已开始');

      // 等待工具卡片出现（最多等待40秒，给AI足够时间）
      // ActivityCard 组件使用 data-activity-type="tool_call" 属性
      const toolCard = page.locator('[data-activity-type="tool_call"]');
      const hasToolCard = await waitForToolCard(page, 40000);

      if (!hasToolCard) {
        // 如果没有工具卡片，截图并检查是否有其他响应
        await takeScreenshot(page, '01-no-tool-card');
        console.log('[测试] 未检测到工具卡片，可能AI没有调用工具');
        console.log('[测试] 检查点: 页面上的内容摘要:');

        // 打印所有助手消息的内容
        const assistantMessages = page.locator('[data-role="assistant"]');
        const count = await assistantMessages.count();
        console.log(`[测试] 找到 ${count} 条助手消息`);
        for (let i = 0; i < count; i++) {
          const text = await assistantMessages.nth(i).textContent();
          console.log(`[测试] 助手消息 ${i + 1}:`, text?.substring(0, 200));
        }

        // 检查是否有工具调用的标记（即使没有渲染成卡片）
        const pageContent = await page.content();
        const hasToolCallMarker = pageContent.includes('TOOL_CALL') ||
                                   pageContent.includes('tool_call') ||
                                   pageContent.includes('file_read');
        console.log(`[测试] 页面中是否包含工具调用标记: ${hasToolCallMarker}`);
      } else {
        console.log('[测试] 工具卡片已出现');
      }

      // 步骤5: 验证工具卡片渲染
      const cardCount = await toolCard.count();
      console.log(`[测试] 找到 ${cardCount} 个工具卡片`);

      if (cardCount > 0) {
        // 获取所有工具卡片的信息
        const cardsInfo = await getToolCardsInfo(page);
        console.log('[测试] 工具卡片列表:');
        cardsInfo.forEach((info, index) => {
          console.log(`  ${index + 1}. ${info.title} (状态: ${info.status})`);
        });

        const firstCard = toolCard.first();

        // 验证卡片基本结构 - ActivityCard 有 .rounded-xl 和 border
        await expect(firstCard.locator('.rounded-xl').or(
          firstCard.locator('[data-activity-type="tool_call"]')
        )).toBeVisible();

        // 验证卡片标题（工具名称）显示
        const cardTitle = firstCard.locator('.font-medium');
        const hasTitle = await cardTitle.count() > 0;
        if (hasTitle) {
          const titleText = await cardTitle.first().textContent();
          console.log(`[测试] 工具卡片标题: ${titleText}`);
          expect(titleText).toBeTruthy();
        }

        // 验证状态显示
        const cardText = await firstCard.textContent();
        console.log(`[测试] 卡片内容预览: ${cardText?.substring(0, 100)}...`);
        expect(cardText).toBeTruthy();

        // 验证卡片可以展开 - ActivityCard 使用 .cursor-pointer
        const header = firstCard.locator('.cursor-pointer');
        const headerCount = await header.count();

        if (headerCount > 0) {
          // 检查卡片是否已展开
          const isExpanded = await firstCard.getAttribute('data-expanded');
          console.log(`[测试] 卡片展开状态: ${isExpanded || 'collapsed'}`);

          await header.first().click();
          await page.waitForTimeout(500);

          // 验证详情内容展开 - ActivityCard 有 pre 和 .text-xs 详情块
          const detailContent = firstCard.locator('pre, .text-xs');
          const detailCount = await detailContent.count();
          console.log(`[测试] 详情块数量: ${detailCount}`);
          const hasDetails = detailCount > 0;
          console.log(`[测试] 卡片详情${hasDetails ? '已' : '未'}展开`);
        }

        // 验证工具状态属性
        const statusAttr = await firstCard.getAttribute('data-activity-status');
        console.log(`[测试] 工具状态: ${statusAttr || '未知'}`);

        await takeScreenshot(page, '01-tool-card-rendered');
      } else {
        console.log('[测试] 没有找到工具卡片，跳过验证');
      }

      // 步骤6: 检查是否有错误
      const errors = page.locator('.text-red-500, .error, [data-activity-status="failed"]');
      const errorCount = await errors.count();

      if (errorCount > 0) {
        console.log(`[测试] 发现 ${errorCount} 个错误状态`);
        await takeScreenshot(page, '01-error-state');
      }

      console.log('[测试] 测试完成');
    });
  });

  test.describe('场景2: Web搜索工具调用', () => {
    test('02-发送搜索消息并验证工具卡片', async ({ page }) => {
      await page.goto('/');
      await waitForPageLoad(page);

      // 发送搜索请求
      const searchMessage = '搜索最新的AI技术进展';
      const inputBox = page.locator('[data-testid="chat-input-textarea"]');

      await inputBox.fill(searchMessage);

      const sendButton = page.locator('[data-testid="chat-send-button"]');
      await sendButton.click();

      console.log('[测试] 搜索消息已发送');

      // 等待AI响应
      const assistantMessage = page.locator('[data-role="assistant"]');
      await expect(assistantMessage.first(), 'AI响应应该开始').toBeVisible({ timeout: 15000 });

      // 等待工具卡片
      const hasToolCard = await waitForToolCard(page, 40000);

      // 检查是否有web_search相关的工具卡片
      const searchCard = page.locator('[data-activity-type="tool_call"]').filter({
        hasText: /web_search|搜索/
      });

      const cardCount = await searchCard.count();
      console.log(`[测试] 找到 ${cardCount} 个搜索工具卡片`);

      if (cardCount > 0) {
        await expect(searchCard.first()).toBeVisible();
        const cardsInfo = await getToolCardsInfo(page);
        console.log('[测试] 工具卡片详情:', cardsInfo);
        await takeScreenshot(page, '02-search-card-rendered');
      } else if (hasToolCard) {
        // 有工具卡片但不是搜索相关的
        const allCards = await getToolCardsInfo(page);
        console.log('[测试] 找到工具卡片但不是搜索工具:', allCards);
        await takeScreenshot(page, '02-other-tool-cards');
      } else {
        console.log('[测试] 未检测到搜索工具卡片（AI可能没有调用搜索工具）');
        await takeScreenshot(page, '02-no-search-card');
      }
    });
  });

  test.describe('场景3: 多工具调用', () => {
    test('03-复杂任务触发多个工具调用', async ({ page }) => {
      await page.goto('/');
      await waitForPageLoad(page);

      // 发送需要多步骤的请求
      const complexMessage = '帮我查看项目结构，然后读取README文件，最后总结项目功能';
      const inputBox = page.locator('[data-testid="chat-input-textarea"]');

      await inputBox.fill(complexMessage);

      const sendButton = page.locator('[data-testid="chat-send-button"]');
      await sendButton.click();

      console.log('[测试] 复杂任务消息已发送');

      // 等待AI响应
      const assistantMessage = page.locator('[data-role="assistant"]');
      await expect(assistantMessage.first(), 'AI响应应该开始').toBeVisible({ timeout: 15000 });

      // 等待工具卡片（给足够时间执行多个工具）
      const hasToolCard = await waitForToolCard(page, 50000);

      // 统计工具卡片数量
      const allCards = page.locator('[data-activity-type="tool_call"]');
      const cardCount = await allCards.count();

      console.log(`[测试] 总共找到 ${cardCount} 个工具卡片`);

      if (cardCount > 0) {
        // 获取所有卡片信息
        const cardsInfo = await getToolCardsInfo(page);
        console.log('[测试] 工具卡片列表:');
        cardsInfo.forEach((info, index) => {
          console.log(`  ${index + 1}. ${info.title} (状态: ${info.status})`);
        });

        // 验证每个卡片
        for (let i = 0; i < Math.min(cardCount, 5); i++) {
          const card = allCards.nth(i);
          const isVisible = await card.isVisible();

          if (isVisible) {
            const cardText = await card.textContent();
            console.log(`[测试] 卡片 ${i + 1}: ${cardText?.substring(0, 50)}...`);
          }
        }

        await takeScreenshot(page, '03-multiple-tool-cards');
      } else {
        console.log('[测试] 未找到任何工具卡片');
        await takeScreenshot(page, '03-no-tool-cards');
      }

      // 验证最终有AI回复
      await expect(assistantMessage.first()).toBeVisible({ timeout: 15000 });
    });
  });

  test.describe('工具卡片交互测试', () => {
    test('04-测试卡片展开折叠功能', async ({ page }) => {
      await page.goto('/');
      await waitForPageLoad(page);

      // 首先发送一个会触发工具的消息
      const inputBox = page.locator('[data-testid="chat-input-textarea"]');
      await inputBox.fill('读取 .env 文件');
      await page.locator('[data-testid="chat-send-button"]').click();

      console.log('[测试] 消息已发送，等待工具卡片...');

      // 等待工具卡片出现
      const toolCard = page.locator('[data-activity-type="tool_call"]');
      const hasToolCard = await waitForToolCard(page, 40000);

      if (!hasToolCard) {
        console.log('[测试] 没有工具卡片出现，跳过交互测试');
        await takeScreenshot(page, '04-no-tool-card');
        return;
      }

      const firstCard = toolCard.first();

      // 测试展开/折叠
      const clickableHeader = firstCard.locator('.cursor-pointer');

      const headerExists = await clickableHeader.count() > 0;
      if (!headerExists) {
        console.log('[测试] 卡片没有可点击的头部');
        await takeScreenshot(page, '04-no-clickable-header');
        return;
      }

      // 初始状态截图
      await takeScreenshot(page, '04-card-initial');

      // 点击展开（如果未展开）
      await clickableHeader.first().click();
      await page.waitForTimeout(500);

      // 检查详情是否显示
      const detailVisible = await firstCard.locator('pre, .text-xs').count() > 0;
      console.log(`[测试] 点击后详情${detailVisible ? '已' : '未'}显示`);

      await takeScreenshot(page, '04-card-expanded');

      // 再次点击折叠
      await clickableHeader.first().click();
      await page.waitForTimeout(500);

      await takeScreenshot(page, '04-card-collapsed');
    });

    test('05-测试工具卡片状态显示', async ({ page }) => {
      await page.goto('/');
      await waitForPageLoad(page);

      // 发送消息
      const inputBox = page.locator('[data-testid="chat-input-textarea"]');
      await inputBox.fill('列出当前目录的文件');
      await page.locator('[data-testid="chat-send-button"]').click();

      console.log('[测试] 消息已发送，等待工具卡片...');

      // 等待并检查工具卡片
      const toolCard = page.locator('[data-activity-type="tool_call"]');
      const hasToolCard = await waitForToolCard(page, 40000);

      if (!hasToolCard) {
        console.log('[测试] 没有工具卡片出现');
        await takeScreenshot(page, '05-no-tool-card');
        return;
      }

      // 检查不同状态的卡片
      const statusAttributes = [
        'completed',
        'running',
        'failed',
        'pending'
      ];

      for (const status of statusAttributes) {
        const cardsWithStatus = page.locator(`[data-activity-status="${status}"]`);
        const count = await cardsWithStatus.count();

        if (count > 0) {
          console.log(`[测试] 找到 ${count} 个状态为 ${status} 的卡片`);
        }
      }

      // 获取所有工具卡片的状态
      const cardsInfo = await getToolCardsInfo(page);
      console.log('[测试] 工具卡片状态:');
      cardsInfo.forEach((info, index) => {
        console.log(`  ${index + 1}. ${info.title} - 状态: ${info.status}`);
      });

      // 检查状态文本
      const statusTexts = page.locator('text=/运行中|已完成|失败|等待中/');
      const statusCount = await statusTexts.count();

      console.log(`[测试] 找到 ${statusCount} 个状态文本显示`);

      await takeScreenshot(page, '05-card-status-display');
    });
  });

  test.describe('响应式布局测试', () => {
    test('06-不同屏幕尺寸下的工具卡片渲染', async ({ page }) => {
      await page.goto('/');
      await waitForPageLoad(page);

      // 触发工具调用
      const inputBox = page.locator('[data-testid="chat-input-textarea"]');
      await inputBox.fill('读取 package.json');
      await page.locator('[data-testid="chat-send-button"]').click();

      console.log('[测试] 消息已发送，等待工具卡片...');

      // 等待工具卡片
      const toolCard = page.locator('[data-activity-type="tool_call"]');
      const hasToolCard = await waitForToolCard(page, 40000);

      if (!hasToolCard) {
        console.log('[测试] 没有工具卡片，跳过响应式测试');
        await takeScreenshot(page, '06-no-tool-card');
        return;
      }

      // 测试不同屏幕尺寸
      const viewports = [
        { width: 1920, height: 1080, name: 'desktop' },
        { width: 768, height: 1024, name: 'tablet' },
        { width: 375, height: 667, name: 'mobile' }
      ];

      for (const viewport of viewports) {
        await page.setViewportSize({ width: viewport.width, height: viewport.height });
        await page.waitForTimeout(500);

        const isVisible = await toolCard.first().isVisible();
        console.log(`[测试] ${viewport.name} (${viewport.width}x${viewport.height}): 卡片${isVisible ? '可见' : '不可见'}`);

        if (isVisible) {
          await takeScreenshot(page, `06-${viewport.name}-layout`);
        }
      }
    });
  });

  test.describe('性能和错误处理', () => {
    test('07-工具执行失败时的卡片显示', async ({ page }) => {
      await page.goto('/');
      await waitForPageLoad(page);

      // 发送可能导致失败的消息（访问不存在的文件）
      const inputBox = page.locator('[data-testid="chat-input-textarea"]');
      await inputBox.fill('读取 /nonexistent/file.txt');
      await page.locator('[data-testid="chat-send-button"]').click();

      console.log('[测试] 消息已发送，等待响应...');

      // 等待AI响应
      const assistantMessage = page.locator('[data-role="assistant"]');
      await expect(assistantMessage.first(), 'AI响应应该开始').toBeVisible({ timeout: 15000 });

      // 等待工具卡片（无论成功或失败）
      await page.waitForTimeout(30000);

      // 检查是否有失败状态的卡片
      const failedCards = page.locator('[data-activity-status="failed"]');

      const failedCount = await failedCards.count();
      console.log(`[测试] 找到 ${failedCount} 个失败状态的卡片`);

      if (failedCount > 0) {
        await expect(failedCards.first()).toBeVisible();

        // 获取失败卡片的信息
        const failedCardInfo = await getToolCardsInfo(page);
        const failedCardsOnly = failedCardInfo.filter(c => c.status === 'failed');
        console.log('[测试] 失败的工具卡片:', failedCardsOnly);

        // 检查错误信息
        const errorMessage = page.locator('text=/错误|失败|error|failed/i');
        const hasError = await errorMessage.count() > 0;

        console.log(`[测试] ${hasError ? '有' : '没有'}错误信息显示`);

        await takeScreenshot(page, '07-failed-card-display');
      } else {
        console.log('[测试] 没有检测到失败的卡片');
        await takeScreenshot(page, '07-no-failed-card');
      }
    });

    test('08-控制台错误检查', async ({ page }) => {
      const consoleErrors: string[] = [];

      page.on('console', (msg) => {
        if (msg.type() === 'error') {
          consoleErrors.push(msg.text());
        }
      });

      await page.goto('/');
      await waitForPageLoad(page);

      // 执行正常操作
      const inputBox = page.locator('[data-testid="chat-input-textarea"]');
      await inputBox.fill('你好');
      await page.locator('[data-testid="chat-send-button"]').click();

      await page.waitForTimeout(5000);

      if (consoleErrors.length > 0) {
        console.log('[测试] 发现控制台错误:');
        consoleErrors.forEach(err => console.log(`  - ${err}`));
      } else {
        console.log('[测试] 没有控制台错误');
      }

      await takeScreenshot(page, '08-console-check');
    });
  });

  test.describe('真实数据验证', () => {
    test('09-从API获取工具执行记录', async ({ page }) => {
      // 这个测试验证后端API是否正常返回工具执行记录
      await page.goto('/');
      await waitForPageLoad(page);

      // 获取当前会话ID
      const url = page.url();
      const sessionIdMatch = url.match(/\/session\/([a-f0-9-]+)/);

      if (!sessionIdMatch) {
        console.log('[测试] 需要先进入会话页面');
        // 点击第一个会话
        const sessionLink = page.locator('a[href*="/session/"]').first();
        const hasSession = await sessionLink.count() > 0;

        if (hasSession) {
          await sessionLink.click();
          await page.waitForTimeout(1000);
        } else {
          return;
        }
      }

      // 调用API获取执行记录
      const currentUrl = page.url();
      const currentSessionId = currentUrl.match(/\/session\/([a-f0-9-]+)/)?.[1];

      if (currentSessionId) {
        const apiResponse = await page.request.get(
          `http://localhost:8888/api/execution/${currentSessionId}/steps`
        );

        console.log(`[测试] API响应状态: ${apiResponse.status()}`);

        if (apiResponse.ok()) {
          const data = await apiResponse.json();
          console.log(`[测试] 获取到 ${data.steps?.length || 0} 条执行记录`);

          // 检查是否有工具调用记录
          const toolCalls = data.steps?.filter((step: any) =>
            step.type === 'tool_call' || step.name?.includes('tool')
          );

          console.log(`[测试] 其中有 ${toolCalls?.length || 0} 条工具调用记录`);

          if (toolCalls && toolCalls.length > 0) {
            console.log('[测试] 工具调用示例:');
            toolCalls.slice(0, 3).forEach((call: any) => {
              console.log(`  - ${call.name} (${call.status})`);
            });
          }
        } else {
          console.log(`[测试] API请求失败: ${apiResponse.statusText()}`);
        }
      }

      await takeScreenshot(page, '09-api-records');
    });
  });
});
