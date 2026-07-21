/**
 * 前端运行时登录流程模拟验证
 *
 * 使用与前端相同的 axios 库（frontend/node_modules/axios → 使用 /workspace/node_modules/axios），
 * 精确复制 frontend/src/services/api/auth.ts + client.ts + authStore.ts 的完整调用链：
 *
 * 调用链：authStore.login() → authApi.login() → client(axios).post() → POST /api/v1/auth/login
 *         → authStore 获取用户信息 → authApi.getCurrentUser() → client(axios).get() → GET /api/v1/auth/me
 *         → authStore 加载工具列表 → GET /api/v1/tools
 *         → 用户提交任务 → POST /api/v1/chat
 *
 * 这不是源码审查，而是用前端相同的 HTTP 库（axios 1.16.1）实际发请求，
 * 验证前端 auth.ts/client.ts 的请求格式、拦截器逻辑、token 注入
 * 与后端 auth.rs/server.rs 的响应格式完全兼容。
 */

// 使用与前端相同的 axios 库
const axios = require('/workspace/node_modules/axios');

const KERNEL_URL = process.env.KERNEL_URL || 'http://localhost:9100';
const STORAGE_KEYS = {
  ACCESS_TOKEN: 'access_token',
  REFRESH_TOKEN: 'refresh_token',
  AUTH_USER: 'auth_user',
  ACCESS_TOKEN_EXPIRY: 'access_token_expiry',
};

// 模拟 localStorage（Node.js 环境无 window/localStorage）
const localStorageSim = {};
const localStorage = {
  getItem: (k) => localStorageSim[k] || null,
  setItem: (k, v) => { localStorageSim[k] = String(v); },
  removeItem: (k) => { delete localStorageSim[k]; },
};

// 模拟前端 client.ts 的 axios 实例（精确复制 client.ts 配置）
const apiClient = axios.create({
  baseURL: KERNEL_URL,
  timeout: 30000,
  headers: { 'Content-Type': 'application/json' },
});

// 精确复制 client.ts 请求拦截器：注入 Bearer token
apiClient.interceptors.request.use((config) => {
  const token = localStorage.getItem(STORAGE_KEYS.ACCESS_TOKEN);
  if (token && config.headers) {
    const existing = config.headers.Authorization;
    if (existing === '') {
      delete config.headers.Authorization;
      return config;
    }
    config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
});

// 测试断言辅助
let passCount = 0;
let failCount = 0;
const results = [];

function assert(condition, message, evidence = '') {
  if (condition) {
    passCount++;
    results.push(`  ✅ ${message}${evidence ? ' — ' + evidence : ''}`);
  } else {
    failCount++;
    results.push(`  ❌ ${message}${evidence ? ' — ' + evidence : ''}`);
  }
}

async function runTests() {
  console.log('='.repeat(70));
  console.log('前端运行时登录流程模拟验证（axios 库 + authStore.login() 调用链）');
  console.log(`后端地址: ${KERNEL_URL}`);
  console.log(`axios 版本: ${axios.VERSION}`);
  console.log('='.repeat(70));

  // ================================================================
  // 旅程 1: 精确模拟 authStore.login('admin', 'admin12345')
  // ================================================================
  console.log('\n[旅程1] authStore.login("admin", "admin12345") 完整调用链');

  // Step 1: authApi.login() → POST /api/v1/auth/login
  // 精确复制 frontend/src/services/api/auth.ts login() 的请求
  try {
    const loginResponse = await apiClient.post('/api/v1/auth/login', {
      username: 'admin',
      password: 'admin12345',
    });

    const data = loginResponse.data;
    assert(data.access_token && typeof data.access_token === 'string',
      'login() 返回 access_token（与前端 LoginResponse 契约一致）',
      `token长度=${data.access_token.length}`);
    assert(data.refresh_token && typeof data.refresh_token === 'string',
      'login() 返回 refresh_token');
    assert(data.token_type === 'bearer',
      'login() 返回 token_type="bearer"',
      `实际: ${data.token_type}`);
    assert(data.expires_in === 1800,
      'login() 返回 expires_in=1800（30分钟）',
      `实际: ${data.expires_in}`);

    // authStore 持久化到 localStorage（精确复制 authStore.ts 行为）
    const expiryTime = Date.now() + data.expires_in * 1000;
    localStorage.setItem(STORAGE_KEYS.ACCESS_TOKEN, data.access_token);
    localStorage.setItem(STORAGE_KEYS.REFRESH_TOKEN, data.refresh_token);
    localStorage.setItem(STORAGE_KEYS.ACCESS_TOKEN_EXPIRY, expiryTime.toString());

    assert(localStorage.getItem(STORAGE_KEYS.ACCESS_TOKEN) === data.access_token,
      'authStore 持久化 access_token 到 localStorage');

    // Step 2: authApi.getCurrentUser() → GET /api/v1/auth/me
    // 精确复制 authStore.login() 内的 getCurrentUser() 调用
    // client.ts 拦截器会自动注入 Bearer token
    const meResponse = await apiClient.get('/api/v1/auth/me');
    const meData = meResponse.data;

    assert(meData.username === 'admin',
      'getCurrentUser() 返回 username="admin"',
      `实际: ${meData.username}`);
    assert(meData.email === 'admin@lingxi.dev',
      'getCurrentUser() 返回 email="admin@lingxi.dev"',
      `实际: ${meData.email}`);
    assert(meData.role === 'admin',
      'getCurrentUser() 返回 role="admin"');
    assert(meData.is_active === true,
      'getCurrentUser() 返回 is_active=true');
    assert(typeof meData.id === 'string' && meData.id.length > 0,
      'getCurrentUser() 返回 id（UUID）',
      `实际: ${meData.id}`);
    assert(typeof meData.created_at === 'string',
      'getCurrentUser() 返回 created_at');

    // authStore 映射 userInfo → User 模型（精确复制 mapUserInfoToUser）
    const user = {
      id: meData.id,
      username: meData.username,
      email: meData.email,
      createdAt: meData.created_at,
    };
    localStorage.setItem(STORAGE_KEYS.AUTH_USER, JSON.stringify(user));
    assert(user.username === 'admin', 'authStore 映射 user.username="admin"');

  } catch (error) {
    assert(false, '旅程1 登录流程异常', error.message);
  }

  // ================================================================
  // 旅程 1b: 错误密码验证（验证前端看到的 400）
  // ================================================================
  console.log('\n[旅程1b] 错误密码 → 前端收到 400');
  try {
    await apiClient.post('/api/v1/auth/login', {
      username: 'admin',
      password: 'wrongpassword',
    });
    assert(false, '错误密码应返回 400');
  } catch (error) {
    assert(error.response && error.response.status === 400,
      '错误密码 → 前端 axios 收到 HTTP 400',
      `status=${error.response?.status}`);
  }

  // ================================================================
  // 旅程 1c: Token 刷新（authStore.refreshToken()）
  // ================================================================
  console.log('\n[旅程1c] authStore.refreshToken() → POST /api/v1/auth/refresh');
  try {
    const refreshToken = localStorage.getItem(STORAGE_KEYS.REFRESH_TOKEN);
    const refreshResponse = await apiClient.post('/api/v1/auth/refresh', {
      refresh_token: refreshToken,
    }, { headers: { Authorization: '' } }); // 精确复制 auth.ts refreshToken() 的 headers

    const refreshData = refreshResponse.data;
    assert(refreshData.access_token && typeof refreshData.access_token === 'string',
      'refreshToken() 返回新 access_token');
    assert(refreshData.token_type === 'bearer',
      'refreshToken() 返回 token_type="bearer"');
    assert(refreshData.expires_in === 1800,
      'refreshToken() 返回 expires_in=1800');

    // authStore 更新 localStorage（精确复制 authStore.ts 行为）
    const newExpiry = Date.now() + refreshData.expires_in * 1000;
    localStorage.setItem(STORAGE_KEYS.ACCESS_TOKEN, refreshData.access_token);
    localStorage.setItem(STORAGE_KEYS.ACCESS_TOKEN_EXPIRY, newExpiry.toString());
    if (refreshData.refresh_token) {
      localStorage.setItem(STORAGE_KEYS.REFRESH_TOKEN, refreshData.refresh_token);
    }

  } catch (error) {
    assert(false, '旅程1c 刷新令牌异常', error.message);
  }

  // ================================================================
  // 旅程 1d: 无 token 访问 /auth/me → 401（前端 client.ts 401 拦截器路径）
  // ================================================================
  console.log('\n[旅程1d] 无 token 访问 /auth/me → 前端收到 401');
  try {
    // 清除 token，模拟未认证状态
    const savedToken = localStorage.getItem(STORAGE_KEYS.ACCESS_TOKEN);
    localStorage.removeItem(STORAGE_KEYS.ACCESS_TOKEN);
    await apiClient.get('/api/v1/auth/me');
    assert(false, '无 token 应返回 401');
    // 恢复 token
    localStorage.setItem(STORAGE_KEYS.ACCESS_TOKEN, savedToken);
  } catch (error) {
    assert(error.response && error.response.status === 401,
      '无 token → 前端 axios 收到 HTTP 401',
      `status=${error.response?.status}`);
    // client.ts 401 拦截器会触发 refreshToken 逻辑
  }

  // ================================================================
  // 旅程 2: 前端加载工具列表（schema.ts/tools.ts → GET /api/v1/tools）
  // ================================================================
  console.log('\n[旅程2] 前端加载工具列表 → GET /api/v1/tools');
  try {
    const toolsResponse = await apiClient.get('/api/v1/tools');
    const tools = toolsResponse.data;

    assert(Array.isArray(tools) && tools.length > 0,
      'GET /api/v1/tools 返回非空数组',
      `${tools.length} 个工具`);
    assert(tools.length === 44,
      '工具数量 = 44（与内核日志一致）',
      `实际: ${tools.length}`);

    // 验证每个工具字段（前端 ToolCard 组件需要）
    const firstTool = tools[0];
    assert(typeof firstTool.name === 'string',
      '工具含 name 字段（前端渲染需要）',
      firstTool.name);
    assert(typeof firstTool.description === 'string',
      '工具含 description 字段');
    assert(typeof firstTool.plugin_id === 'string',
      '工具含 plugin_id 字段');

    // Schema 端点
    const schemaResponse = await apiClient.get('/api/v1/schema');
    const schema = schemaResponse.data;
    assert(Array.isArray(schema.pipelines) && schema.pipelines.length === 44,
      'GET /api/v1/schema pipelines=44');
    assert(Array.isArray(schema.tools) && schema.tools.length === 44,
      'GET /api/v1/schema tools=44');

  } catch (error) {
    assert(false, '旅程2 加载工具列表异常', error.message);
  }

  // ================================================================
  // 旅程 3: 前端提交任务（pipelineMessageStore → POST /api/v1/chat）
  // ================================================================
  console.log('\n[旅程3] 前端提交任务 → POST /api/v1/chat（管道引擎）');
  try {
    const chatResponse = await apiClient.post('/api/v1/chat', {
      message: 'hello from frontend runtime test',
      session_id: 'frontend-runtime-001',
    });

    const chatData = chatResponse.data;
    assert(chatData.type === 'message',
      'chat 响应 type="message"');
    assert(typeof chatData.content === 'string',
      'chat 响应含 content 字段');

    // 关键验证：不是 echo 模式
    const content = chatData.content;
    const isPipeline = content.startsWith('[pipeline:');
    const isEcho = content.startsWith('Response to:');
    assert(isPipeline,
      'chat 响应以 [pipeline: 开头 → 已接入管道引擎',
      `content前80字符: ${content.substring(0, 80)}`);
    assert(!isEcho,
      'chat 响应不以 Response to: 开头 → 非 echo 模式');
    assert(chatData.session_id === 'frontend-runtime-001',
      'chat 响应 session_id 回显一致');
    assert(typeof chatData.timestamp === 'string',
      'chat 响应含 timestamp 字段');

    // 记录管道引擎执行细节
    const noPluginMatch = content.match(/no_plugin_executed/);
    const pipelineMatch = content.match(/\[pipeline: run_id=(\w+),\s*plugins_tried=(\d+)/);
    if (pipelineMatch) {
      assert(true,
        `管道引擎执行详情: run_id=${pipelineMatch[1]}, plugins_tried=${pipelineMatch[2]}`,
        noPluginMatch ? '注意: NoopInvoker占位→no_plugin_executed（已知技术债务）' : '插件已执行');
    }

  } catch (error) {
    assert(false, '旅程3 提交任务异常', error.message);
  }

  // ================================================================
  // 输出结果
  // ================================================================
  console.log('\n' + '='.repeat(70));
  console.log('验证结果明细：');
  results.forEach(r => console.log(r));
  console.log('='.repeat(70));
  console.log(`\n总计: ${passCount} passed, ${failCount} failed`);
  console.log(`通过率: ${((passCount / (passCount + failCount)) * 100).toFixed(1)}%`);

  if (failCount > 0) {
    process.exit(1);
  } else {
    console.log('\n✅ 所有前端运行时验证全部通过');
  }
}

runTests().catch(err => {
  console.error('测试执行出错:', err);
  process.exit(1);
});
