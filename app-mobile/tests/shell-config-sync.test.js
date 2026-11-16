'use strict';
/*
 * 移动壳 apiBase 漂移闸契约测试（纯 node 运行，零第三方依赖）：
 *   node app-mobile/tests/shell-config-sync.test.js
 * 执法核 = syncweb --check（fixture 副本对账，不动真树）。覆盖：
 *   一致态绿（≥2 组区分度输入：云基址 / 回环基址）/ 生成物漂移红（删值实验）/
 *   声明面缺真值红 / 生成物缺格式红 / 注入标签缺失红 / env 注入值不参与对账 /
 *   www 整树缺席绿（ADR 2026-10-16：gitignored 构建产物，干净 checkout 形态）。
 */
const fs = require('fs');
const os = require('os');
const path = require('path');
const { spawnSync } = require('child_process');
const assert = require('assert');

const syncwebPath = path.join(__dirname, '..', 'scripts', 'syncweb.mjs');

function makeFixture(declaredApiBase, generatedApiBase, withTag, opts = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'shell-config-sync-'));
  const capConfig = {
    appId: 'com.lingxi.agentos',
    plugins: declaredApiBase === null
      ? { ShellConfig: {} }
      : { ShellConfig: { apiBase: declaredApiBase } },
  };
  fs.writeFileSync(
    path.join(root, 'capacitor.config.json'),
    JSON.stringify(capConfig, null, 2) + '\n'
  );
  if (opts.omitWww) return root; // 干净 checkout 形态：www/ 整树不生成
  fs.mkdirSync(path.join(root, 'www'), { recursive: true });
  if (generatedApiBase !== null) {
    const body = opts.brokenFormat
      ? '/* 手改破坏格式 */\nwindow.OTHER = 1;\n'
      : '/* 由 scripts/syncweb.mjs 生成，勿手改 */\n'
        + `window.LINGXI_SHELL_CONFIG = { apiBase: ${JSON.stringify(generatedApiBase)}, versionName: "9.9", versionCode: 1 };\n`;
    fs.writeFileSync(path.join(root, 'www', 'shell-config.js'), body);
  }
  const tag = '<script src="/shell-config.js"></script>';
  fs.writeFileSync(
    path.join(root, 'www', 'index.html'),
    `<!doctype html><head>${withTag === false ? '' : tag}</head><body></body>`
  );
  return root;
}

function runCheck(root, env) {
  return spawnSync(process.execPath, [syncwebPath, '--check', '--root', root], {
    encoding: 'utf8',
    env: { ...process.env, ...env },
  });
}

/* 1. 一致态绿——区分度输入组 1：云基址（公网 URL 形态） */
let root = makeFixture('http://cloud.example', 'http://cloud.example', true);
let r = runCheck(root, {});
assert.strictEqual(r.status, 0, `一致态应绿: ${r.stderr}`);
assert.ok(r.stdout.includes('绿'), '绿态应输出绿标记');

/* 2. 一致态绿——区分度输入组 2：回环基址（本机内核形态，端口面） */
fs.rmSync(root, { recursive: true, force: true });
root = makeFixture('http://127.0.0.1:9300', 'http://127.0.0.1:9300', true);
r = runCheck(root, {});
assert.strictEqual(r.status, 0, `回环一致态应绿: ${r.stderr}`);

/* 3. env 注入值不参与对账（--check 只认声明面；env 属构建缝双模式） */
r = runCheck(root, { LINGXI_API_BASE: 'http://elsewhere.example' });
assert.strictEqual(r.status, 0, 'env 注入值不得影响对账');

/* 4. 生成物漂移红（删值实验：生成物 apiBase 改值） */
fs.rmSync(root, { recursive: true, force: true });
root = makeFixture('http://cloud.example', 'http://127.0.0.1:9300', true);
r = runCheck(root, {});
assert.strictEqual(r.status, 1, '生成物漂移应红');
assert.ok(r.stderr.includes('漂移'), '漂移红应指明漂移项');

/* 5. 声明面缺真值红（删值实验：删 plugins.ShellConfig.apiBase） */
fs.rmSync(root, { recursive: true, force: true });
root = makeFixture(null, 'http://cloud.example', true);
r = runCheck(root, {});
assert.strictEqual(r.status, 1, '声明面缺真值应红');
assert.ok(r.stderr.includes('声明面'), '缺声明面应指明声明面缺真值');

/* 6. www 整树缺席绿（ADR 2026-10-16：gitignored 构建产物，干净 checkout 形态，
 *    无入库面可对账；声明面仍恒执法——组 5 已钉） */
fs.rmSync(root, { recursive: true, force: true });
root = makeFixture('http://cloud.example', null, true, { omitWww: true });
r = runCheck(root, {});
assert.strictEqual(r.status, 0, `www 整树缺席应绿: ${r.stderr}`);
assert.ok(r.stdout.includes('www 未生成'), '缺席绿应说明无入库面可对账');

/* 7. 生成物在场但格式破坏红（在场才执法：文件存在但无冻结格式声明） */
fs.rmSync(root, { recursive: true, force: true });
root = makeFixture('http://cloud.example', 'ignored-when-broken', true, { brokenFormat: true });
r = runCheck(root, {});
assert.strictEqual(r.status, 1, '生成物格式破坏应红');
assert.ok(r.stderr.includes('shell-config.js'), '格式破坏红应指明文件');

/* 8. 注入标签缺失红（删值实验：index.html 无标签） */
fs.rmSync(root, { recursive: true, force: true });
root = makeFixture('http://cloud.example', 'http://cloud.example', false);
r = runCheck(root, {});
assert.strictEqual(r.status, 1, '注入标签缺失应红');
assert.ok(r.stderr.includes('标签'), '标签缺失应指明注入链断裂');

fs.rmSync(root, { recursive: true, force: true });
console.log('shell-config-sync 契约测试：8 组全过（一致绿×2 + env 不参与 + www 缺席绿 + 漂移/缺面/缺格式/缺标签 红×4）');
