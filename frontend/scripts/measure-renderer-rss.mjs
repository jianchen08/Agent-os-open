/**
 * renderer RSS 复测口径（renderer 内存优化前后对照）
 *
 * 原理：经 CDP 连到运行中的前端（Electron renderer 或普通 Chromium 均可），
 * SystemInfo.getProcessInfo 给出各 renderer 进程 PID，再经 OS 查 working set
 * （tasklist/ps）；同时经 performance.memory 取 JS 堆（对照，堆不得劣化）。
 * 注：新版 Chrome 的该 CDP 接口已不含内存字段，故拆 PID/内存两步。
 *
 * 前置：前端以远程调试端口启动——
 * - Electron 主进程加启动参 --remote-debugging-port=9222；
 * - 普通 Chromium 调试 dev 页面： chrome --remote-debugging-port=9222 <url>
 *
 * 用法（frontend/ 下）：
 *   CDP_URL=http://127.0.0.1:9222 node scripts/measure-renderer-rss.mjs \
 *     --label "优化后-全负载" --duration 20000
 *
 * 场景口径（与归因实测一致，前后必须同口径）：
 *   A. 欢迎页+单会话   —— 基线态（实测 330MB）
 *   B. 全负载          —— 监控图表 + 任务面板（117 条）+ 100 会话列表 + 消息流
 *                         （实测 437MB；优化目标 ≤330MB）
 *   采样 duration 毫秒，取 renderer working set 峰值/末值。
 */
import { execFileSync } from 'node:child_process'
import { chromium } from '@playwright/test'

function arg(name, fallback) {
  const i = process.argv.indexOf(`--${name}`)
  return i > -1 ? process.argv[i + 1] : fallback
}

/** OS 级查进程 working set（MB）；进程已退出返回 null */
function rssMB(pid) {
  try {
    if (process.platform === 'win32') {
      const out = execFileSync('tasklist', ['/FI', `PID eq ${pid}`, '/FO', 'CSV', '/NH'], {
        encoding: 'utf8',
      })
      const m = out.match(/"([\d.,\s]+)\s?K"/i)
      if (!m) return null
      return Math.round(Number(m[1].replace(/[.,\s]/g, '')) / 1024)
    }
    const out = execFileSync('ps', ['-o', 'rss=', '-p', String(pid)], { encoding: 'utf8' })
    const kb = Number(out.trim())
    return Number.isFinite(kb) ? Math.round(kb / 1024) : null
  } catch {
    return null
  }
}

const cdpUrl = process.env.CDP_URL ?? 'http://127.0.0.1:9222'
const label = arg('label', 'sample')
const durationMs = Number(arg('duration', 15000))

const browser = await chromium.connectOverCDP(cdpUrl)

async function sampleRenderers() {
  const session = await browser.newBrowserCDPSession()
  const info = await session.send('SystemInfo.getProcessInfo')
  await session.detach()
  const entries = info.processes ?? info.processInfo ?? []
  const out = []
  for (const p of entries) {
    if (String(p.type).toLowerCase() !== 'renderer') continue
    const mb = rssMB(p.id)
    if (mb != null) out.push({ pid: p.id, rssMB: mb })
  }
  return out.sort((a, b) => b.rssMB - a.rssMB)
}

async function jsHeapMB() {
  let maxHeap = 0
  for (const ctx of browser.contexts()) {
    for (const page of ctx.pages()) {
      try {
        const m = await page.evaluate(() => performance.memory)
        const used = m?.usedJSHeapSize
        if (typeof used === 'number' && Number.isFinite(used)) {
          maxHeap = Math.max(maxHeap, used)
        }
      } catch {
        // 某些 target（webview/扩展页）不可 evaluate，跳过
      }
    }
  }
  return maxHeap > 0 ? Math.round(maxHeap / 1024 / 1024) : null
}

console.log(`[${label}] 连接 ${cdpUrl}，采样 ${durationMs}ms ...`)
const samples = []
const endAt = Date.now() + durationMs
while (Date.now() < endAt) {
  const rs = await sampleRenderers()
  if (rs.length > 0) samples.push(rs)
  await new Promise((r) => setTimeout(r, 500))
}

if (samples.length === 0) {
  console.error('未采样到 renderer 进程：确认 CDP 端口与页面已打开')
  process.exit(1)
}

const peak = Math.max(...samples.map((s) => s[0].rssMB))
const last = samples.at(-1)[0].rssMB
const heap = await jsHeapMB()

console.log(`[${label}]`)
console.log(`  renderer RSS 峰值: ${peak} MB`)
console.log(`  renderer RSS 末值: ${last} MB`)
console.log(`  JS 堆 usedJSHeapSize: ${heap ?? '不可用（页面未暴露 performance.memory）'} MB（对照指标，不得劣化）`)
console.log(`  采样帧数: ${samples.length}`)

await browser.close()
