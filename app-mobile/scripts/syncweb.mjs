#!/usr/bin/env node
/**
 * syncweb —— 移动壳「本地直用」web 资产同步链（构建 → 拷贝 → sync）。
 *
 * 用法：
 *   cd app-mobile && npm run syncweb
 * 前置：frontend/ 依赖已安装（脚本内部执行 `npm run build`）。
 *
 * 步骤：
 *   1. 构建 frontend dist（vite build）；
 *   2. 清空 app-mobile/www 后整拷 frontend/dist/*（哈希文件名，不清会累积陈旧 chunk）；
 *   3. 从 capacitor.config.json plugins.ShellConfig.apiBase 生成 www/shell-config.js
 *      （值源声明单点；生成物勿手改）；
 *   4. 向 www/index.html head 注入 <script src="/shell-config.js">——head 经典脚本
 *      先于 deferred 模块脚本执行，保证前端读基址时 window.LINGXI_SHELL_CONFIG 已就绪；
 *      仅改壳侧拷贝，云端 dist 不动；
 *   5. npx cap sync android（www → android assets/public，capacitor.config.json 随包）。
 *
 * Capacitor 8 无 window.Capacitor.getConfig()（JS 运行时未提供，配置读取只在原生侧），
 * 本脚本的生成物即其等价注入机制；选型记录见 E轨迹汇聚落地_台账 2026-10-13 本地直用刀。
 */
import { spawnSync } from 'node:child_process'
import { cpSync, existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const shellRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const frontendRoot = resolve(shellRoot, '..', 'frontend')
const wwwDir = join(shellRoot, 'www')
const distDir = join(frontendRoot, 'dist')

function run(cmd, args, opts = {}) {
  const res = spawnSync(cmd, args, {
    stdio: 'inherit',
    shell: process.platform === 'win32',
    ...opts,
  })
  if (res.status !== 0) {
    console.error(`[syncweb] \`${cmd} ${args.join(' ')}\` 失败（exit ${res.status}），中止`)
    process.exit(res.status ?? 1)
  }
}

// ── --check 模式（D1①② 漂移闸执法核，零副作用对账）──────────────────────
// 用法：node scripts/syncweb.mjs --check [--root <壳根目录副本>]
// 对账三路：①声明面 plugins.ShellConfig.apiBase 存在且非空；②生成物
// www/shell-config.js 的 apiBase ≡ 声明面（env 注入值 LINGXI_API_BASE 属构建
// 缝双模式，不得残留在库）；③www/index.html 注入标签在场。
// www/ 缺席语义（ADR 2026-10-16-build-artifacts-out-of-git）：www 是 gitignored
// 可再生构建产物，干净 checkout 上天然缺席——缺席=无入库面可漂移，跳过该项
// 对账（①恒执法，②③仅生成物在场时执法）；在场而不一致仍退出 1——fail-closed
// 对"可执法面"不豁免，闸不退化为永绿假闸（声明面缺失照红）。--root 供闸测试
// 喂 fixture 副本（临时目录对账，不动真树）；执法入口 scripts/check_shell_config_sync.py。
const SHELL_TAG = '<script src="/shell-config.js"></script>'
// 生成物为两行冻结格式（首行注释 + 单行声明），语句锚不落串首，取无锚匹配
const GENERATED_API_BASE_RE = /window\.LINGXI_SHELL_CONFIG = \{ apiBase: "((?:[^"\\]|\\.)*)"/

function shellConfigSyncFindings(rootDir) {
  const findings = []
  let declared = null
  try {
    const capConfig = JSON.parse(readFileSync(join(rootDir, 'capacitor.config.json'), 'utf8'))
    const apiBase = capConfig?.plugins?.ShellConfig?.apiBase
    if (typeof apiBase === 'string' && apiBase) {
      declared = apiBase
    } else {
      findings.push('capacitor.config.json 缺 plugins.ShellConfig.apiBase（非空 string）——声明面缺真值')
    }
  } catch (err) {
    findings.push(`capacitor.config.json 读取/解析失败: ${err.message}`)
  }
  // www/ 两文件缺席（ENOENT）= gitignored 构建产物未生成，无入库面可对账，跳过；
  // 在场才执法——与头注 ADR 2026-10-16 语义一致
  try {
    const js = readFileSync(join(rootDir, 'www', 'shell-config.js'), 'utf8')
    const generated = (js.match(GENERATED_API_BASE_RE) || [])[1]
    if (generated === undefined) {
      findings.push('www/shell-config.js 不含冻结格式的 apiBase 声明（生成物缺失或被手改破坏格式）——跑 npm run syncweb 重生成')
    } else if (declared !== null && generated !== declared) {
      findings.push(
        `apiBase 漂移：声明面 ${JSON.stringify(declared)} ≠ 生成物 ${JSON.stringify(generated)}` +
        '——生成链未随声明面重跑，或 env 注入值（LINGXI_API_BASE）残留入库；跑 npm run syncweb 后按打包链惯例处置 www',
      )
    }
  } catch (err) {
    if (err.code !== 'ENOENT') findings.push(`www/shell-config.js 读取失败: ${err.message}`)
  }
  try {
    const html = readFileSync(join(rootDir, 'www', 'index.html'), 'utf8')
    if (!html.includes(SHELL_TAG)) {
      findings.push('www/index.html 缺 shell-config 注入标签——注入链断裂，跑 npm run syncweb 修复')
    }
  } catch (err) {
    if (err.code !== 'ENOENT') findings.push(`www/index.html 读取失败: ${err.message}`)
  }
  return findings
}

function runCheck() {
  const argv = process.argv.slice(2)
  const rootArg = argv.indexOf('--root')
  const rootDir = rootArg !== -1 ? resolve(argv[rootArg + 1]) : shellRoot
  const findings = shellConfigSyncFindings(rootDir)
  if (findings.length > 0) {
    console.error(`[syncweb --check] 漂移闸红（${findings.length} 项）：`)
    for (const finding of findings) console.error(`  - ${finding}`)
    process.exit(1)
  }
  const wwwJsExists = existsSync(join(rootDir, 'www', 'shell-config.js'))
  const wwwHtmlExists = existsSync(join(rootDir, 'www', 'index.html'))
  if (wwwJsExists && wwwHtmlExists) {
    console.log('[syncweb --check] 绿：声明面 apiBase ≡ 生成物，注入标签在场')
  } else if (wwwJsExists || wwwHtmlExists) {
    console.log('[syncweb --check] 绿：声明面在场，www 生成物部分在场已对账（缺席面无入库面可漂移）')
  } else {
    console.log('[syncweb --check] 绿：声明面在场；www 未生成（gitignored 构建产物），无入库面可对账')
  }
}

if (process.argv.slice(2).includes('--check')) {
  runCheck() // 红路径内部 process.exit(1)；绿路径必须显式退出——落空即进构建链重写 www/
  process.exit(0)
}

// 1. 构建前端
console.log('[syncweb] 1/5 构建 frontend dist ...')
run('npm', ['run', 'build'], { cwd: frontendRoot })
if (!existsSync(join(distDir, 'index.html'))) {
  console.error('[syncweb] 构建后未找到 frontend/dist/index.html，中止')
  process.exit(1)
}

// 2. 清空 www 后整拷 dist
console.log('[syncweb] 2/5 拷贝 dist → www ...')
rmSync(wwwDir, { recursive: true, force: true })
mkdirSync(wwwDir, { recursive: true })
cpSync(distDir, wwwDir, { recursive: true })

// 3. 生成 shell-config.js（值源 = capacitor.config.json + android/app/build.gradle）
// 安全注：apiBase 现为公网明文 HTTP（无 TLS 中间人面；androidScheme http 同为
// 明文，是混合内容拦截不成立的前提）——明文 HTTP 过渡态，TLS 待立项
// （审查总册 P1-14③，需拍板节奏），本生成链如实保留现状。
console.log('[syncweb] 3/5 生成 www/shell-config.js ...')
const capConfig = JSON.parse(readFileSync(join(shellRoot, 'capacitor.config.json'), 'utf8'))
// 环境变量优先（G-M0 构建缝双模式：云端=配置真值原样；本机内核=构建方注入
// http://127.0.0.1:<port>，值源声明单点仍是本脚本的生成物），未注入走配置真值。
const apiBase = process.env.LINGXI_API_BASE || capConfig?.plugins?.ShellConfig?.apiBase
if (typeof apiBase !== 'string') {
  console.error('[syncweb] capacitor.config.json 缺 plugins.ShellConfig.apiBase（string），中止')
  process.exit(1)
}
// 版本面（关于页「应用版本」数据源）：真值 = build.gradle 单点，避免双处维护漂移
const gradleText = readFileSync(join(shellRoot, 'android', 'app', 'build.gradle'), 'utf8')
const versionCode = Number((gradleText.match(/versionCode\s+(\d+)/) || [])[1])
const versionName = (gradleText.match(/versionName\s+"([^"]+)"/) || [])[1]
if (!Number.isInteger(versionCode) || typeof versionName !== 'string') {
  console.error('[syncweb] android/app/build.gradle 解析 versionCode/versionName 失败，中止')
  process.exit(1)
}
writeFileSync(
  join(wwwDir, 'shell-config.js'),
  `/* 由 scripts/syncweb.mjs 生成，勿手改；值源 = capacitor.config.json plugins.ShellConfig + android/app/build.gradle */\nwindow.LINGXI_SHELL_CONFIG = { apiBase: ${JSON.stringify(apiBase)}, versionName: ${JSON.stringify(versionName)}, versionCode: ${versionCode} };\n`,
)
// 3b. 原生可读 sidecar（cap sync 随 www 整拷进 assets/public）：双模式真值进包
// 后原生侧可辨别——MainActivity 换装的 ShellConfigWebViewClient 运行时读它：
// source=env-override → 采信本文件 apiBase（构建方 LINGXI_API_BASE 注入的本机
// 内核回环，此时包内声明面仍是云端值）；source=declared → 采信 capacitor.config.json
// 声明面。运行时 apiBase 从此钉在原生配置面，生成物/残留文件的 apiBase 值不再
// 被消费；sidecar 缺失（旧生成链产物）原生侧按声明面兜底。
const envOverride = Boolean(process.env.LINGXI_API_BASE)
writeFileSync(
  join(wwwDir, 'shell-config.properties'),
  '# 由 scripts/syncweb.mjs 生成，勿手改；原生侧运行时读取（ShellConfigWebViewClient），键契约见 ShellConfigResolver\n'
  + `# source=declared|env-override：env-override=构建方 LINGXI_API_BASE 注入（本机内核档），原生侧优先采信；declared=与 capacitor.config.json 声明面同值，原生侧采信声明面\n`
  + `apiBase=${apiBase}\nsource=${envOverride ? 'env-override' : 'declared'}\n`,
)

// 4. 向 www/index.html head 注入 shell-config script 标签
console.log('[syncweb] 4/5 注入 index.html script 标签 ...')
const indexHtmlPath = join(wwwDir, 'index.html')
const TAG = '<script src="/shell-config.js"></script>'
let html = readFileSync(indexHtmlPath, 'utf8')
if (html.includes(TAG)) {
  console.warn('[syncweb] index.html 已含 shell-config 标签，跳过注入')
} else {
  const headIdx = html.indexOf('<head>')
  if (headIdx === -1) {
    console.error('[syncweb] index.html 无 <head>，无法注入，中止')
    process.exit(1)
  }
  const at = headIdx + '<head>'.length
  html = `${html.slice(0, at)}\n    <!-- 移动壳本地直用：API 基址注入（值源 capacitor.config.json，由 syncweb 生成） -->\n    ${TAG}${html.slice(at)}`
  writeFileSync(indexHtmlPath, html)
}

// 5. cap sync android
console.log('[syncweb] 5/5 cap sync android ...')
run('npx', ['cap', 'sync', 'android'], { cwd: shellRoot })

console.log(`[syncweb] 完成：apiBase=${JSON.stringify(apiBase)} version=${versionName}(${versionCode}) 已进包`)
