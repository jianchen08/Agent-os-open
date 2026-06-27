import { test, expect, type Page } from '@playwright/test';

const API_BASE = 'http://localhost:8988';
const APP_URL = 'http://localhost:5188';

async function adminLogin(page: Page) {
  const loginResp = await page.request.post(`${API_BASE}/api/v1/auth/login`, {
    data: { username: 'admin', password: 'admin123' },
  });
  expect(loginResp.ok(), 'admin登录应成功').toBe(true);
  const tokens = await loginResp.json();
  await page.goto(APP_URL);
  await page.evaluate((t) => {
    localStorage.setItem('access_token', t.access_token);
    localStorage.setItem('refresh_token', t.refresh_token);
    localStorage.setItem('access_token_expiry', String(Date.now() + 3600000));
    localStorage.setItem('auth_user', JSON.stringify({ id: 'admin', username: 'admin', email: 'admin@example.com', created_at: new Date().toISOString() }));
  }, tokens);
  await page.reload();
  await page.waitForLoadState('networkidle');
  await page.waitForTimeout(2000);
}

test('WS实时工具调用: toolHandler 构造 tool_call part 字段正确', async ({ page }) => {
  const wsErrors: string[] = [];
  page.on('pageerror', (err) => wsErrors.push('PAGE_ERROR: ' + err.message));

  await adminLogin(page);

  // 监听 window 上的 WS 消息（通过 patch WebSocket 构造函数捕获 tool_start/tool_result）
  // 然后通过 store 发送消息触发工具调用，验证 part 构造
  const result = await page.evaluate(async () => {
    const pipelineMod = await import('/src/stores/pipelineMessageStore.ts');

    // 1. 创建新会话（通过 API）
    const loginResp = await fetch('/api/v1/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: 'admin', password: 'admin123' }),
    });
    const { access_token } = await loginResp.json();

    const createResp = await fetch('/api/v1/threads', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${access_token}` },
      body: JSON.stringify({ intent: 'WS工具调用验证' }),
    });
    const thread = await createResp.json();
    const tid = thread.thread_id;
    const pid = thread.pipeline_ids[0];

    // 2. 注册管道到 store
    const store = pipelineMod.usePipelineMessageStore.getState();
    store.registerPipeline({
      pipelineId: pid, sessionId: tid, level: 1, tabId: `main-${tid}`,
      agentName: '灵汐', status: 'running', parentId: null, unreadCount: 0,
    });
    store.activatePipeline(pid);

    // 3. 通过 GlobalWebSocket 发送消息触发工具调用
    const wsMod = await import('/src/services/websocket/GlobalWebSocket');
    const globalWS = (wsMod as any).globalWS;

    // 等待 WS 真正连接（最多 8 秒）
    let wsConnected = false;
    for (let i = 0; i < 16; i++) {
      if (globalWS.status === 'connected' || globalWS.isConnected) { wsConnected = true; break; }
      await new Promise((r) => setTimeout(r, 500));
    }

    // 发送会触发工具调用的消息（sendUserInput 签名: threadId, content, opts）
    globalWS.sendUserInput(tid, '请用 bash_execute 列出当前目录文件', { pipelineId: pid });

    // 4. 等待工具调用产生（监听 store 的消息变化）
    let toolPart: any = null;
    for (let i = 0; i < 40; i++) {
      await new Promise((r) => setTimeout(r, 1000));
      const msgs = store.getMessages(pid);
      for (const m of msgs) {
        const parts = m.parts || [];
        for (const p of parts) {
          if (p.type === 'tool_call') {
            toolPart = p;
            break;
          }
        }
        if (toolPart) break;
      }
      if (toolPart) break;
    }

    return {
      tid, pid,
      wsConnected,
      wsStatus: globalWS.status,
      hasToolPart: toolPart !== null,
      toolPartCallId: toolPart?.callId,
      toolPartName: toolPart?.name,
      toolPartArgs: toolPart?.args,
      toolPartState: toolPart?.state,
      msgCount: store.getMessages(pid).length,
    };
  });

  console.log('=== WS实时工具调用验证结果 ===');
  console.log(JSON.stringify(result, null, 2));

  // 验证 toolHandler 从 WS 事件正确构造了 tool_call part
  expect(result.hasToolPart, '应产生 tool_call part').toBe(true);
  expect(result.toolPartCallId, 'part.callId 应非空（WS tool_start.call_id 映射）').toBeTruthy();
  expect(result.toolPartName, 'part.name 应非空（WS tool_start.tool_name 映射）').toBeTruthy();
  expect(result.msgCount, '应产生消息').toBeGreaterThan(0);

  // 无致命错误
  const fatal = wsErrors.filter((e) => !e.includes('WebSocket') && !e.includes('502'));
  expect(fatal, '不应有致命JS错误').toHaveLength(0);
}, 90000);
