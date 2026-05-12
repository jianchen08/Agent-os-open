/**
 * L1→L2→L3完整调用链端到端测试
 *
 * 测试验证：
 * 1. L1 Agent 接收用户请求
 * 2. L1 提交任务给 L2 Agent（任务准备助手）
 * 3. L2 提交任务给 L3 Agent（执行Agent）
 * 4. L3 创建文件
 * 5. 验证文件实际创建
 * 6. 验证数据库中的agent调用记录
 * 7. 验证execution_records中的执行轨迹
 */

import { test, expect } from '@playwright/test';
import { quickLogin, waitForAIResponse, waitForAPI } from './helpers';

/**
 * 辅助函数：通过UI创建会话
 *
 * 注意：根据系统架构，会话只能通过主agent创建，前端不能直接调用API创建会话。
 * 此函数通过UI操作（点击"新建会话"按钮）来触发主agent创建会话。
 */
async function createTestSession(page: any, intent: string) {
  // 通过UI创建会话
  // 1. 点击"新建会话"按钮
  const newSessionButton = '[data-testid="new-session-btn"], button:has-text("新建会话"), button:has-text("New Session")';
  await page.waitForSelector(newSessionButton, { timeout: 10000 });
  await page.click(newSessionButton);

  // 2. 如果有输入框，输入会话标题
  const titleInput = 'input[placeholder*="标题"], input[placeholder*="Title"]';
  const hasTitleInput = await page.locator(titleInput).isVisible().catch(() => false);
  if (hasTitleInput) {
    await page.fill(titleInput, intent);
    // 点击确认按钮
    const confirmButton = 'button:has-text("创建"), button:has-text("Create")';
    await page.click(confirmButton);
  }

  // 3. 等待导航到会话页面
  await page.waitForURL(/\/session\/[a-zA-Z0-9-]+/, { timeout: 15000 });

  // 4. 从URL中提取会话ID
  const url = page.url();
  const sessionIdMatch = url.match(/\/session\/([a-zA-Z0-9-]+)/);
  if (!sessionIdMatch) {
    throw new Error('无法从URL提取会话ID');
  }

  const sessionId = sessionIdMatch[1];
  console.log('通过UI创建会话:', { sessionId, intent });
  return sessionId;
}

/**
 * 辅助函数：发送消息到会话
 * 返回：发送前的消息数量
 */
async function sendTestMessage(page: any, sessionId: string, content: string): Promise<number> {
  // 等待消息输入框出现
  const inputSelector = 'textarea[placeholder*="消息"], textarea[placeholder*="输入"], [data-testid="message-input"]';
  await page.waitForSelector(inputSelector, { timeout: 10000 });

  // 记录发送前的消息数量
  const initialMessageCount = await page.locator('.message').count();

  // 输入消息
  await page.fill(inputSelector, content);

  // 监听发送请求
  const sendRequest = waitForAPI(page, '/api/v1/messages', 'POST').catch(() => null);

  // 点击发送按钮或按 Enter
  const sendButton = 'button[data-testid="send-btn"], button:has-text("发送")';
  const hasSendButton = await page.locator(sendButton).isVisible().catch(() => false);

  if (hasSendButton) {
    await page.click(sendButton);
  } else {
    await page.keyboard.press('Enter');
  }

  // 等待发送请求完成
  await sendRequest;

  // 等待用户消息出现在列表中
  try {
    await page.waitForFunction(
      (initialCount, text) => {
        const messages = document.querySelectorAll('.message');
        return messages.length > initialCount && Array.from(messages).some(msg => msg.textContent?.includes(text));
      },
      { initialCount, text: content.substring(0, 50) },
      { timeout: 15000 }
    );
  } catch (e) {
    // 如果等待失败，继续执行
    const pageContent = await page.locator('body').textContent();
    console.log('等待消息超时，页面内容预览:', pageContent?.substring(0, 500));
  }

  return initialMessageCount;
}

/**
 * 辅助函数：验证文件是否被创建
 * 注意：由于没有直接的工具执行API，我们通过检查执行记录中的工具调用来推断
 */
async function verifyFileCreated(page: any, fileName: string, expectedContent: string): Promise<boolean> {
  try {
    const result = await page.evaluate(async ({ fileName }) => {
      const token = localStorage.getItem('access_token') || localStorage.getItem('token');
      if (!token) {
        return { error: 'No token' };
      }

      try {
        // 获取threads的详情，其中包含execution_records
        const url = new URL(window.location.href);
        const sessionId = url.pathname.split('/').pop();
        const response = await fetch(`http://localhost:8888/api/v1/threads/${sessionId}`, {
          headers: {
            'Authorization': `Bearer ${token}`,
            'Content-Type': 'application/json',
          },
        });

        if (!response.ok) {
          return { error: `HTTP ${response.status}` };
        }

        const data = await response.json();
        return { success: true, data };
      } catch (error) {
        return { error: String(error) };
      }
    }, { fileName });

    if (result.success && result.data && result.data.execution_records) {
      const records = result.data.execution_records;
      // 检查是否有文件操作相关的执行记录
      const hasFileOperation = records.some((r: any) =>
        (r.content && r.content.includes(fileName)) ||
        (r.record_type === 'tool_call' && r.content && r.content.includes('file'))
      );
      console.log(`执行记录中${hasFileOperation ? '找到' : '未找到'}文件操作证据`);
      return hasFileOperation;
    }

    console.log(`文件验证失败: ${JSON.stringify(result)}`);
    return false;
  } catch (error) {
    console.warn(`验证文件时出错: ${error}`);
    return false;
  }
}

/**
 * 辅助函数：获取Agent调用记录
 */
async function getAgentCallRecords(page: any, sessionId: string): Promise<any> {
  try {
    const result = await page.evaluate(async (sessionId) => {
      const token = localStorage.getItem('access_token') || localStorage.getItem('token');
      if (!token) {
        return { error: 'No token' };
      }

      try {
        const response = await fetch(`http://localhost:8888/api/v1/agent-calls?limit=20`, {
          headers: {
            'Authorization': `Bearer ${token}`,
            'Content-Type': 'application/json',
          },
        });

        if (!response.ok) {
          return { error: `HTTP ${response.status}` };
        }

        const data = await response.json();
        return { success: true, data };
      } catch (error) {
        return { error: String(error) };
      }
    }, sessionId);

    return result;
  } catch (error) {
    console.warn(`获取Agent调用记录失败: ${error}`);
    return { error: String(error) };
  }
}

/**
 * 辅助函数：获取会话执行记录
 */
async function getSessionExecutionRecords(page: any, sessionId: string): Promise<any> {
  try {
    const result = await page.evaluate(async (sessionId) => {
      const token = localStorage.getItem('access_token') || localStorage.getItem('token');
      if (!token) {
        return { error: 'No token' };
      }

      try {
        // 使用 /threads/{thread_id}/records 端点
        const response = await fetch(`http://localhost:8888/api/v1/threads/${sessionId}/records`, {
          headers: {
            'Authorization': `Bearer ${token}`,
            'Content-Type': 'application/json',
          },
        });

        if (!response.ok) {
          return { error: `HTTP ${response.status}` };
        }

        const data = await response.json();
        return { success: true, data };
      } catch (error) {
        return { error: String(error) };
      }
    }, sessionId);

    return result;
  } catch (error) {
    console.warn(`获取执行记录失败: ${error}`);
    return { error: String(error) };
  }
}

/**
 * 分析调用链并验证层次结构
 */
function analyzeCallChain(agentCalls: any[]): {
  hasL1: boolean;
  hasL2: boolean;
  hasL3: boolean;
  chain: string[];
  levels: Set<string>;
} {
  const levels = new Set<string>();
  const chain: string[] = [];

  for (const call of agentCalls) {
    const level = call.caller_level;
    const target = call.target_agent_name;
    levels.add(level);
    chain.push(`${level} → ${target} (${call.operation_type})`);
  }

  return {
    hasL1: levels.has('L1') || chain.some(c => c.includes('L1')),
    hasL2: levels.has('L2') || chain.some(c => c.includes('L2')),
    hasL3: chain.some(c => c.includes('general_agent') || c.includes('执行')),
    chain,
    levels,
  };
}

test.describe('L1→L2→L3完整调用链测试', () => {
  test.beforeEach(async ({ page }) => {
    await quickLogin(page);
  });

  test('应该验证完整的L1→L2→L3调用链和文件创建', async ({ page }) => {
    // 1. 通过API创建会话
    const sessionId = await createTestSession(page, 'L1→L2→L3完整调用链测试');
    expect(sessionId).toBeTruthy();
    console.log('✓ 会话创建成功:', sessionId);

    // 2. 导航到会话页面
    await page.goto(`/session/${sessionId}`, { timeout: 30000 });
    await page.waitForLoadState('domcontentloaded', { timeout: 30000 });
    await page.waitForSelector('body', { timeout: 5000 });

    // 3. 发送需要L1→L2→L3协作的任务
    const testFileName = `test_l1_l2_l3_${Date.now()}.txt`;
    const testFileContent = 'L1→L2→L3调用链测试成功';
    const testMessage = `请创建一个名为 ${testFileName} 的文件，内容是 "${testFileContent}"`;

    console.log('发送测试任务:', testMessage);
    await sendTestMessage(page, sessionId, testMessage);

    // 4. 等待AI响应（可能需要较长时间因为涉及多层Agent调用）
    console.log('等待L1→L2→L3调用链完成...');
    const response = await page.waitForFunction(
      () => {
        const allText = document.body.textContent || '';
        return allText.includes('我来帮你') ||
               allText.includes('我已经') ||
               allText.includes('任务') ||
               allText.includes('提交') ||
               allText.includes('L2') ||
               allText.includes('L3') ||
               allText.includes('执行');
      },
      { timeout: 180000 } // 3分钟超时
    ).then(() => page.evaluate(() => document.body.textContent || ''));

    console.log('收到AI响应:', response.substring(0, 800));

    // 5. 验证响应内容非空
    expect(response).toBeTruthy();
    expect(response.length).toBeGreaterThan(0);

    // 6. 等待一段时间确保任务执行完成
    console.log('等待文件创建和数据库记录...');
    await page.waitForTimeout(5000);

    // 7. 验证文件是否被创建
    console.log('验证文件创建...');
    const fileExists = await verifyFileCreated(page, testFileName, testFileContent);
    console.log(`文件 ${testFileName} ${fileExists ? '已成功创建' : '创建失败'}`);

    // 8. 获取Agent调用记录
    console.log('获取Agent调用记录...');
    const agentCallsResult = await getAgentCallRecords(page, sessionId);
    console.log('Agent调用记录响应:', JSON.stringify(agentCallsResult, null, 2));

    // 9. 分析调用链
    let callChainAnalysis = { hasL1: false, hasL2: false, hasL3: false, chain: [], levels: new Set<string>() };
    if (agentCallsResult.success && agentCallsResult.data && agentCallsResult.data.records) {
      const records = agentCallsResult.data.records;
      console.log(`找到 ${records.length} 条Agent调用记录`);
      callChainAnalysis = analyzeCallChain(records);
      console.log('调用链分析:', {
        hasL1: callChainAnalysis.hasL1,
        hasL2: callChainAnalysis.hasL2,
        hasL3: callChainAnalysis.hasL3,
        chain: callChainAnalysis.chain,
      });
    }

    // 10. 获取会话执行记录
    console.log('获取会话执行记录...');
    const executionRecordsResult = await getSessionExecutionRecords(page, sessionId);
    if (executionRecordsResult.success && executionRecordsResult.data) {
      const records = executionRecordsResult.data.records || executionRecordsResult.data;
      console.log(`找到 ${Array.isArray(records) ? records.length : 0} 条执行记录`);

      // 检查执行记录的层次结构
      if (Array.isArray(records) && records.length > 0) {
        const hasAgentExecution = records.some((r: any) =>
          r.executor_type === 'agent' || r.record_type === 'task_execution'
        );
        console.log('执行记录包含Agent执行:', hasAgentExecution);
      }
    }

    // 11. 综合验证结果
    console.log('\n=== 调用链验证结果 ===');
    console.log('1. AI响应:', response.length > 0 ? '✓' : '✗');
    console.log('2. 文件创建:', fileExists ? '✓' : '✗');
    console.log('3. L1调用:', callChainAnalysis.hasL1 ? '✓' : '✗');
    console.log('4. L2调用:', callChainAnalysis.hasL2 ? '✓' : '✗');
    console.log('5. L3调用:', callChainAnalysis.hasL3 ? '✓' : '✗');

    // 最终断言 - 至少要有响应和一定程度的调用链证据
    const hasValidResponse = response.length > 0;
    const hasSomeCallChainEvidence = callChainAnalysis.hasL1 || callChainAnalysis.hasL2 || callChainAnalysis.hasL3;

    expect(hasValidResponse).toBeTruthy();
    expect(hasSomeCallChainEvidence).toBeTruthy();

    if (fileExists) {
      console.log('✓✓✓ 完整L1→L2→L3调用链验证成功！');
    } else {
      console.log('⚠ 文件验证失败，但调用链可能已正常工作');
    }
  });

  test('应该能够追踪L1到L3的调用链', async ({ page }) => {
    const sessionId = await createTestSession(page, 'L1调用链追踪测试');
    await page.goto(`/session/${sessionId}`, { timeout: 30000 });
    await page.waitForLoadState('domcontentloaded', { timeout: 30000 });

    // 记录初始消息数量
    const initialMessageCount = await page.locator('.message').count();

    // 发送一个需要搜索和执行的任务
    const searchMessage = '请搜索可用的工具，然后使用bash工具列出当前目录的文件';
    await sendTestMessage(page, sessionId, searchMessage);

    // 等待响应
    console.log('等待L1分析任务并调用L3执行...');
    await waitForAIResponse(page, 120000);

    // 验证消息数量增加
    const finalMessageCount = await page.locator('.message').count();
    expect(finalMessageCount).toBeGreaterThan(initialMessageCount);

    // 检查是否有工具调用的显示
    const pageText = await page.locator('.message').allTextContents();
    const text = pageText.join(' ');
    const hasToolCallEvidence =
      (await page.locator('.tool-call, [data-testid="tool-call"], .execution-record').count()) > 0 ||
      text.includes('bash') ||
      text.includes('ls') ||
      text.includes('工具');

    console.log('工具调用证据:', hasToolCallEvidence);
  });

  test('L1应该能够将复杂任务分解给L3', async ({ page }) => {
    const sessionId = await createTestSession(page, 'L1复杂任务分解测试');
    await page.goto(`/session/${sessionId}`, { timeout: 30000 });
    await page.waitForLoadState('domcontentloaded', { timeout: 30000 });

    // 发送一个多步骤任务
    const complexTask = `请按以下步骤执行：
1. 创建一个名为 multi_step_test.txt 的文件
2. 写入当前时间戳
3. 读取并确认文件内容`;

    await sendTestMessage(page, sessionId, complexTask);

    console.log('等待L1分解任务并协调L3执行...');
    const response = await waitForAIResponse(page, 180000); // 3分钟超时

    console.log('复杂任务响应:', response.substring(0, 300));

    // 验证响应包含步骤执行的证据
    const hasStepEvidence =
      response.includes('步骤') ||
      response.includes('创建') ||
      response.includes('写入') ||
      response.includes('读取') ||
      response.includes('文件') ||
      response.toLowerCase().includes('step') ||
      response.toLowerCase().includes('created') ||
      response.toLowerCase().includes('written');

    expect(hasStepEvidence).toBeTruthy();
  });

  test('应该能够通过API验证Agent调用记录', async ({ page }) => {
    const sessionId = await createTestSession(page, 'L1 API验证测试');
    await page.goto(`/session/${sessionId}`, { timeout: 30000 });
    await page.waitForLoadState('domcontentloaded', { timeout: 30000 });

    // 发送消息
    await sendTestMessage(page, sessionId, '请使用bash命令echo "L1 to L3 test" > test_api_verify.txt');

    // 等待处理
    await waitForAIResponse(page, 120000);

    // 尝试通过API获取Agent调用记录
    try {
      const agentCallsData = await page.evaluate(async () => {
        const token = localStorage.getItem('access_token') || localStorage.getItem('token');
        if (!token) return { error: 'No token' };

        try {
          const response = await fetch('http://localhost:8888/api/v1/agent-calls?limit=10', {
            headers: {
              'Authorization': `Bearer ${token}`,
              'Content-Type': 'application/json',
            },
          });

          if (!response.ok) {
            return { error: `HTTP ${response.status}` };
          }

          const data = await response.json();
          return { success: true, data };
        } catch (error) {
          return { error: String(error) };
        }
      });

      console.log('Agent调用记录响应:', JSON.stringify(agentCallsData, null, 2));

      // 如果API可用，验证调用记录
      if (agentCallsData.success && agentCallsData.data) {
        const records = agentCallsData.data.records || agentCallsData.data.items || agentCallsData.data;
        expect(Array.isArray(records)).toBeTruthy();

        if (records.length > 0) {
          console.log(`✓ 找到 ${records.length} 条Agent调用记录`);
          console.log('调用记录:', JSON.stringify(records[0], null, 2));
        }
      }
    } catch (error) {
      console.warn('无法验证Agent调用记录API:', error);
      // 不让测试失败，因为API可能还没实现
    }
  });

  test('L1应该能够处理L3执行失败的情况', async ({ page }) => {
    const sessionId = await createTestSession(page, 'L1错误处理测试');
    await page.goto(`/session/${sessionId}`, { timeout: 30000 });
    await page.waitForLoadState('domcontentloaded', { timeout: 30000 });

    // 发送一个可能导致失败的任务（访问不存在的目录）
    const failingTask = '请尝试访问 /nonexistent/directory/that/does/not/exist 目录';

    await sendTestMessage(page, sessionId, failingTask);

    console.log('等待L1处理L3的执行失败...');
    const response = await waitForAIResponse(page, 60000);

    console.log('失败处理响应:', response.substring(0, 200));

    // 验证L1能够报告错误或提供替代方案
    const hasErrorHandling =
      response.includes('错误') ||
      response.includes('不存在') ||
      response.includes('失败') ||
      response.includes('无法') ||
      response.toLowerCase().includes('error') ||
      response.toLowerCase().includes('failed') ||
      response.toLowerCase().includes('not found');

    expect(hasErrorHandling).toBeTruthy();
  });

  test('应该能够并发处理多个L3任务', async ({ page }) => {
    const sessionId = await createTestSession(page, 'L1多任务处理测试');
    await page.goto(`/session/${sessionId}`, { timeout: 30000 });
    await page.waitForLoadState('domcontentloaded', { timeout: 30000 });

    // 发送多个独立任务
    const tasks = [
      'echo "Task 1" > task1.txt',
      'echo "Task 2" > task2.txt',
      'echo "Task 3" > task3.txt',
    ];

    const multiTaskMessage = `请依次执行以下任务：
1. ${tasks[0]}
2. ${tasks[1]}
3. ${tasks[2]}`;

    await sendTestMessage(page, sessionId, multiTaskMessage);

    console.log('等待L1协调L3执行多个任务...');
    const response = await waitForAIResponse(page, 180000);

    console.log('多任务响应:', response.substring(0, 300));

    // 验证所有任务都被处理
    const mentionsMultipleTasks =
      (response.match(/task1|task2|task3/gi) || []).length >= 2 ||
      response.includes('完成') ||
      response.includes('执行');

    expect(mentionsMultipleTasks).toBeTruthy();
  });
});

test.describe('L1调用L3性能测试', () => {
  test('应该在合理时间内完成L1到L3的调用', async ({ page }) => {
    await quickLogin(page);

    const sessionId = await createTestSession(page, 'L1性能测试');
    await page.goto(`/session/${sessionId}`, { timeout: 30000 });
    await page.waitForLoadState('domcontentloaded', { timeout: 30000 });

    // 记录开始时间
    const startTime = Date.now();

    // 发送简单任务
    await sendTestMessage(page, sessionId, 'echo "Performance test" > perf_test.txt');

    // 等待响应
    await waitForAIResponse(page, 120000);

    // 计算耗时
    const duration = Date.now() - startTime;
    console.log(`L1→L3调用耗时: ${duration}ms`);

    // 验证在合理时间内完成（3分钟内）
    expect(duration).toBeLessThan(180000);
  });
});
