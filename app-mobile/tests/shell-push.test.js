'use strict';
/*
 * shell-push.js 去重内核契约测试（纯 node 运行，零第三方依赖）：
 *   node app-mobile/tests/shell-push.test.js
 * 覆盖：新 id 触发 / 已见 id 抑制 / 同任务不同状态为不同键 / 首启快照不轰炸 /
 *       空增量静默 / 容量截断留新 / 区分度输入组 / localStorage 往返 /
 *       目标跟随 + token 换发 / 状态栏明暗映射与主题跟随（注入对齐/双向/免重发/可恢复）。
 */
const fs = require('fs');
const path = require('path');
const assert = require('assert');

const scriptPath = path.join(
  __dirname,
  '..',
  'android',
  'app',
  'src',
  'main',
  'assets',
  'shell-push.js'
);
const source = fs.readFileSync(scriptPath, 'utf8');

/* 最小沙盒：脚本顶层 setup() 完整执行（fetch 桩回 401 静默跳过轮询）；
 * stub 定时器防挂起；documentElement/classList 与 MutationObserver 桩供主题跟随用例 */
const store = {};
const sandboxWindow = {};
const rootClasses = {};
let setStyleCalls = [];
let mutObserverCallback = null;
let mutObserverTarget = null;
let mutObserverOptions = null;
global.window = sandboxWindow;
global.window.Capacitor = {
  Plugins: {
    StatusBar: {
      setStyle: (opts) => {
        setStyleCalls.push(opts);
        return Promise.resolve();
      },
    },
  },
};
global.document = {
  visibilityState: 'hidden',
  documentElement: {
    classList: {
      contains: (c) => !!rootClasses[c],
      add: (c) => { rootClasses[c] = true; },
      remove: (c) => { delete rootClasses[c]; },
    },
  },
};
global.MutationObserver = class {
  constructor(cb) { mutObserverCallback = cb; }
  observe(target, options) {
    mutObserverTarget = target;
    mutObserverOptions = options;
  }
};
global.localStorage = {
  getItem: (k) => (Object.prototype.hasOwnProperty.call(store, k) ? store[k] : null),
  setItem: (k, v) => {
    store[k] = String(v);
  },
};
global.setInterval = () => 0;
global.setTimeout = () => 0;
global.fetch = () => Promise.resolve({ status: 401, json: () => Promise.resolve({}) });

eval(source);

const api = sandboxWindow.LingxiShellPush;
assert(api && typeof api.diffSeen === 'function', 'LingxiShellPush API 未暴露');

/* 1. 新 id 触发，并集入已见 */
let r = api.diffSeen(['req-a', 'req-b'], ['req-b', 'req-c', 'req-d'], 200);
assert.deepStrictEqual(r.news, ['req-c', 'req-d'], '新 id 应触发通知');
assert.deepStrictEqual(r.updatedSeen, ['req-a', 'req-b', 'req-c', 'req-d']);

/* 2. 已见 id 抑制（不重复通知） */
r = api.diffSeen(['task-1|completed', 'task-2|failed'], ['task-1|completed', 'task-2|failed'], 200);
assert.deepStrictEqual(r.news, []);

/* 3. 同任务不同状态 = 不同键（completed → failed 新触发） */
r = api.diffSeen(['task-1|completed'], ['task-1|failed'], 200);
assert.deepStrictEqual(r.news, ['task-1|failed']);

/* 4. 首启快照：现状全标已见，零通知 */
r = api.diffSeen(null, ['t1|completed', 't2|failed', 'req-x'], 200);
assert.deepStrictEqual(r.news, [], '首启快照不得产生任何通知');
assert.deepStrictEqual(r.updatedSeen, ['t1|completed', 't2|failed', 'req-x']);

/* 5. 建快照后空增量静默 */
r = api.diffSeen(['req-a'], [], 200);
assert.deepStrictEqual(r.news, []);
assert.deepStrictEqual(r.updatedSeen, ['req-a']);

/* 6. 容量截断：保留最新（尾部），最旧被逐出 */
r = api.diffSeen(['1', '2', '3'], ['4', '5'], 4);
assert.deepStrictEqual(r.updatedSeen, ['2', '3', '4', '5'], '最旧条目应被截断');
assert.strictEqual(r.updatedSeen.length, 4);
assert.deepStrictEqual(r.news, ['4', '5']);

/* 7. 区分度输入组：纯数字键 */
r = api.diffSeen(['9001', '9002'], ['9002', '9003'], 200);
assert.deepStrictEqual(r.news, ['9003']);

/* 8. localStorage 往返 + 缺键语义（null = 无快照） */
api.saveSeen('lingxi_seen_pending', ['a', 'b']);
assert.deepStrictEqual(api.loadSeen('lingxi_seen_pending'), ['a', 'b']);
assert.strictEqual(api.loadSeen('lingxi_never_saved'), null, '未存过快照应返回 null');

/* 9. 通知 id 稳定且为非负 int32 */
const h1 = api.stringHash('some-request-id');
const h2 = api.stringHash('some-request-id');
assert.strictEqual(h1, h2, '同键哈希须稳定');
assert(h1 >= 0 && h1 <= 0x7fffffff, '哈希须落在非负 int32');

/* ── v1.4 根修：目标跟随 + token 换发 ──
 * fetch 在本沙盒默认 undefined（setup 早退）；以下用例按需注入 fetch 桩。
 * 换发链路是异步的：整体包 async IIFE，全部断言通过后打印总结。 */
(async () => {

/* 10. 目标基址优先级：用户设置（非空）> 构建期注入；空白/缺失回落注入值 */
assert.strictEqual(api.pickBaseUrl('http://user.example', 'http://default.example'), 'http://user.example');
assert.strictEqual(api.pickBaseUrl('   ', 'http://default.example'), 'http://default.example');
assert.strictEqual(api.pickBaseUrl(null, 'http://default.example'), 'http://default.example');
assert.strictEqual(api.pickBaseUrl(undefined, 'http://default.example'), 'http://default.example');

/* 10b. configBase：读构建期注入 window.LINGXI_SHELL_CONFIG.apiBase；缺失/空/非串 = null */
assert.strictEqual(api.configBase(), null, '无注入须返回 null（不回落硬编码地址）');
sandboxWindow.LINGXI_SHELL_CONFIG = { apiBase: 'http://shellcfg.example' };
assert.strictEqual(api.configBase(), 'http://shellcfg.example', '注入值须原样取用');
sandboxWindow.LINGXI_SHELL_CONFIG = { apiBase: '' };
assert.strictEqual(api.configBase(), null, '空串注入视同无注入');
sandboxWindow.LINGXI_SHELL_CONFIG = { apiBase: 123 };
assert.strictEqual(api.configBase(), null, '非串注入视同无注入');
sandboxWindow.LINGXI_SHELL_CONFIG = { apiBase: 'http://shellcfg.example' };

/* 11. currentBase：读 localStorage server_base_url，未设置回落构建期注入 */
assert.strictEqual(api.currentBase(), 'http://shellcfg.example', '未设置须回落 shell-config 注入基址');
store['server_base_url'] = 'http://custom.example';
assert.strictEqual(api.currentBase(), 'http://custom.example', '用户设置须生效（与连接页同键联动）');
delete store['server_base_url'];
sandboxWindow.LINGXI_SHELL_CONFIG = null;
assert.strictEqual(api.currentBase(), null, '无用户设置且无注入 = 无基址（轮询跳过本轮）');
sandboxWindow.LINGXI_SHELL_CONFIG = { apiBase: 'http://shellcfg.example' };

/* 11b. 防残留锁：壳层脚本不得再内置硬编码基址（基址唯一来源=构建期注入） */
assert.ok(!source.includes('203.0.113.153'), '壳层不得残留硬编码云基址');

/* 11c. tokenKeyFor：refresh token 按后端分键（与前端 shellApiBase.refreshTokenStorageKey
 * 同契约）——默认档（当前基址=构建期注入）裸键；自定义后端 refresh_token@<base> */
assert.strictEqual(api.tokenKeyFor('http://shellcfg.example'), 'refresh_token', '默认档（本机内核）须沿用裸键');
assert.strictEqual(api.tokenKeyFor(null), 'refresh_token', '无基址视同默认档裸键');
assert.strictEqual(
  api.tokenKeyFor('http://custom.example'),
  'refresh_token@http://custom.example',
  '自定义后端须按基址分键'
);
assert.notStrictEqual(
  api.tokenKeyFor('http://a.example'),
  api.tokenKeyFor('http://b.example'),
  '不同后端基址须不同键（登录态互不串写）'
);

/* 12. token 换发：POST refresh 端点、access 仅回调内存、新 refresh_token 写回原键 */
{
  let captured = null;
  global.fetch = (url, opts) => {
    captured = { url, opts };
    return Promise.resolve({
      status: 200,
      json: () => Promise.resolve({ access_token: 'at-1', refresh_token: 'rt-new', expires_in: 900 }),
    });
  };
  store['refresh_token'] = 'rt-old';
  const got = [];
  await new Promise((res) => {
    api.refreshAccessToken((token) => { got.push(token); res(); });
  });
  assert.strictEqual(captured.url, 'http://shellcfg.example/api/v1/auth/refresh', '须打 refresh 端点（基址=构建期注入）');
  assert.strictEqual(captured.opts.method, 'POST');
  assert.deepStrictEqual(JSON.parse(captured.opts.body), { refresh_token: 'rt-old' });
  assert.deepStrictEqual(got, ['at-1'], 'access token 须经回调交付（仅内存）');
  assert.strictEqual(store['refresh_token'], 'rt-new', '轮换的新 refresh_token 必须写回原键');
}

/* 13. 换发 401 → cb(null) 静默跳过；不写回任何值 */
{
  global.fetch = () => Promise.resolve({ status: 401, json: () => Promise.resolve({}) });
  store['refresh_token'] = 'rt-stale';
  const got401 = await new Promise((res) => {
    api.refreshAccessToken((token) => res(token));
  });
  assert.strictEqual(got401, null, '401 须回调 null（跳过本轮）');
  assert.strictEqual(store['refresh_token'], 'rt-stale', '失败不得写回');
}

/* 14. 无 refresh_token → cb(null)（未登录等待态），且不发请求 */
{
  delete store['refresh_token'];
  let fetchCalled = false;
  global.fetch = () => { fetchCalled = true; return Promise.resolve({ status: 200 }); };
  const noToken = await new Promise((res) => {
    api.refreshAccessToken((token) => res(token));
  });
  assert.strictEqual(noToken, null, '无凭据须回调 null');
  assert.strictEqual(fetchCalled, false, '无凭据不得发请求');
}

/* 14b. 自定义后端档：换发读写都在分键 refresh_token@<base> 上，裸键不受扰 */
{
  global.fetch = () =>
    Promise.resolve({
      status: 200,
      json: () => Promise.resolve({ access_token: 'at-remote', refresh_token: 'rt-remote-new', expires_in: 900 }),
    });
  store['server_base_url'] = 'http://custom.example';
  store['refresh_token@http://custom.example'] = 'rt-remote-old';
  store['refresh_token'] = 'rt-local-keep';
  const remoteGot = await new Promise((res) => {
    api.refreshAccessToken((token) => res(token));
  });
  assert.strictEqual(remoteGot, 'at-remote', '自定义后端换发须正常交付 access token');
  assert.strictEqual(
    store['refresh_token@http://custom.example'],
    'rt-remote-new',
    '轮换新值须写回分键'
  );
  assert.strictEqual(store['refresh_token'], 'rt-local-keep', '本机档裸键不得被远端轮换覆盖');
  delete store['server_base_url'];
  delete store['refresh_token@http://custom.example'];
  delete store['refresh_token'];
}

/* ── v1.7：状态栏图标明暗随 app 主题（@capacitor/status-bar）── */

/* 14c. 后端模式判定（isRemoteMode）：remote 模式=用户设置非空（与连接设置页同键
 * 同判）——该模式壳层跳过前台服务（FGS 为保活本地栈存在）；local 模式照旧 */
assert.strictEqual(api.isRemoteMode(), false, '无用户设置 = local 模式（FGS 照旧）');
store['server_base_url'] = 'http://remote.example:19300';
assert.strictEqual(api.isRemoteMode(), true, '用户设置指向远端 = remote 模式');
store['server_base_url'] = '   ';
assert.strictEqual(api.isRemoteMode(), false, '空白设置视同未设置 = local 模式');
delete store['server_base_url'];

/* 15. 纯函数映射：dark→DARK(浅色图标)/无 dark→LIGHT(深色图标)；输出互异且恒在枚举内 */
assert.strictEqual(api.statusBarStyle(true), 'DARK', '暗色主题须要 DARK 样式（浅色图标）');
assert.strictEqual(api.statusBarStyle(false), 'LIGHT', '浅色主题须要 LIGHT 样式（深色图标）');
const styleEnum = new Set([api.statusBarStyle(true), api.statusBarStyle(false)]);
assert.deepStrictEqual(styleEnum, new Set(['DARK', 'LIGHT']), '两态输出须互异且覆盖枚举');

/* 16. 注入即对齐现状：沙盒初始无 dark class → 首次调用即发 LIGHT；
 *     observer 挂在 documentElement 上、只盯 class 属性 */
assert.strictEqual(setStyleCalls.length, 1, '注入时应恰好对齐一次现状');
assert.deepStrictEqual(setStyleCalls[0], { style: 'LIGHT' }, '浅色现状须下发 LIGHT');
assert.strictEqual(mutObserverTarget, global.document.documentElement, 'observer 须挂根节点');
assert.deepStrictEqual(mutObserverOptions, { attributes: true, attributeFilter: ['class'] });

/* 17. 切暗色（模拟 themeService.applyTheme 写 html.dark）→ 下发 DARK */
const flush = () => new Promise((r) => setImmediate(r));
rootClasses.dark = true;
mutObserverCallback();
await flush();
assert.deepStrictEqual(setStyleCalls[setStyleCalls.length - 1], { style: 'DARK' }, '暗色须下发 DARK');

/* 18. 切回浅色 → 双向跟随，下发 LIGHT */
delete rootClasses.dark;
mutObserverCallback();
await flush();
assert.deepStrictEqual(setStyleCalls[setStyleCalls.length - 1], { style: 'LIGHT' }, '回浅色须下发 LIGHT');

/* 19. 无关 class 写入（如 no-glassmorphism）不改变明暗 → 不重发原生调用 */
const callsBeforeUnrelated = setStyleCalls.length;
mutObserverCallback();
await flush();
assert.strictEqual(setStyleCalls.length, callsBeforeUnrelated, '明暗未变不得重发 setStyle');

/* 20. 原生拒绝：不 throw，且失败不记 last → 下轮变化可重试（错误可恢复） */
const originalSetStyle = sandboxWindow.Capacitor.Plugins.StatusBar.setStyle;
sandboxWindow.Capacitor.Plugins.StatusBar.setStyle = () => Promise.reject(new Error('native boom'));
rootClasses.dark = true;
mutObserverCallback();
await flush();
sandboxWindow.Capacitor.Plugins.StatusBar.setStyle = originalSetStyle;
mutObserverCallback();
await flush();
assert.deepStrictEqual(
  setStyleCalls[setStyleCalls.length - 1],
  { style: 'DARK' },
  '失败后须可重试成功'
);

/* 21. StatusBar 插件缺席（桩未生成）：不 throw、不记调用 */
delete sandboxWindow.Capacitor.Plugins.StatusBar;
rootClasses.dark = false;
mutObserverCallback();
await flush();
delete rootClasses.dark;
sandboxWindow.Capacitor.Plugins.StatusBar = { setStyle: originalSetStyle };

console.log('shell-push 去重内核契约测试：22 组全过');
})().catch((e) => { console.error(e); process.exit(1); });
