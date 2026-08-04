// 前端卡顿诊断脚本（Playwright + CDP 驱动）
// 用法: node scripts/probe_jank.mjs
// 采集: ① CDP Network.webSocketFrame* 拦截所有 WS（含已建立连接）→ 消息频率/类型/payload
//       ② 注入页面探针 → 长任务、帧率、丢帧
//       ③ Performance trace → 火焰图归因（可用 chrome devtools 打开 jank-trace.zip）
//       ④ HTTP 请求监控 → 确认消息是否真的发出

import { chromium } from 'playwright'
import { writeFileSync, mkdirSync } from 'fs'
import { join } from 'path'

const FRONTEND = process.env.PROBE_FRONTEND || 'http://localhost:5290'
const USERNAME = process.env.PROBE_USER || 'admin'
const PASSWORD = process.env.PROBE_PASS || 'admin123'
const OUT_DIR = process.env.PROBE_OUT || join(process.cwd(), 'probe-out')
const HEADLESS = process.env.PROBE_HEADLESS !== '0'
const PROMPT = process.env.PROBE_PROMPT ||
  '请写一段较长的内容：用 markdown 输出 3 段文字、一个包含 20 行代码的 python 代码块、一个二级标题、和一个 3x3 的表格。要求内容充实，总字数 600 字以上。'
const STREAM_WAIT_MS = Number(process.env.PROBE_STREAM_WAIT || 45000)

mkdirSync(OUT_DIR, { recursive: true })

// 页面探针：长任务 + 帧率（WS 由 CDP 采集，不在页面内包裹）
const PROBE_CODE = `
window.__probe = { startedAt: performance.now(), longTasks: [], frames: 0, jankyFrames: 0 };
(function(){
  if (window.__probeActive) return;
  window.__probeActive = true;
  const S = window.__probe;
  try {
    new PerformanceObserver((list)=>{
      for (const e of list.getEntries()) if (e.duration > 30)
        S.longTasks.push({t:Math.round(e.duration), name:e.name, at:Math.round(e.startTime)});
    }).observe({ entryTypes: ['longtask'] });
  } catch(_) {}
  let last = performance.now();
  function loop(){
    if (!window.__probeActive) return;
    S.frames++;
    const now = performance.now(), gap = now - last;
    if (gap > 25) {
      S.jankyFrames++;
      if (gap > 60 && S.longTasks.length < 3000)
        S.longTasks.push({t:Math.round(gap), name:'raf-gap', at:Math.round(now - S.startedAt)});
    }
    last = now;
    requestAnimationFrame(loop);
  }
  requestAnimationFrame(loop);
})();
`

async function main() {
  console.log(`[probe] 启动 Chromium (headless=${HEADLESS})`)
  const browser = await chromium.launch({ headless: HEADLESS })
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } })
  await context.addInitScript(PROBE_CODE)
  const page = await context.newPage()

  // ---------- CDP: 监听 WS 流量（核心，能拦已建立的连接）----------
  const client = await context.newCDPSession(page)
  await client.send('Network.enable')

  // CDP Tracing：拿完整火焰图。用 ReportEvents 模式，dataCollected 事件累积数据。
  const traceEvents = []
  const tracingCompletePromise = new Promise((resolve) => {
    client.on('Tracing.dataCollected', ({ value }) => {
      for (const ev of value) traceEvents.push(ev)
    })
    client.once('Tracing.tracingComplete', () => resolve())
  })

  const wsStats = {
    requestIdToUrl: {},
    framesReceived: [],
    byType: {},
    totalBytes: 0,
    totalCount: 0,
  }
  client.on('Network.webSocketCreated', ({ requestId, url }) => {
    wsStats.requestIdToUrl[requestId] = url
  })
  client.on('Network.webSocketFrameReceived', ({ timestamp, response }) => {
    const raw = response.payloadData
    const size = raw.length
    let type = null
    try { const p = JSON.parse(raw); type = p?.type || null } catch (_) {}
    wsStats.framesReceived.push({ ts: timestamp, size, type })
    wsStats.totalBytes += size
    wsStats.totalCount++
    if (type) wsStats.byType[type] = (wsStats.byType[type] || 0) + 1
  })
  client.on('Network.webSocketFrameSent', ({ response }) => {
    let type = null
    try { type = JSON.parse(response.payloadData)?.type } catch (_) {}
    if (type && type !== 'heartbeat' && type !== 'ping') {
      console.log(`[ws→] 发送: type=${type}`)
    }
  })

  // ---------- HTTP 请求监控 ----------
  const httpReqs = []
  page.on('request', (req) => {
    const u = req.url()
    if (u.includes('/api/v1/') && !u.includes('themes')) {
      httpReqs.push({ method: req.method(), url: u.replace(FRONTEND, ''), t: Date.now() })
    }
  })
  page.on('response', (resp) => {
    const u = resp.url()
    if (u.includes('/messages') || u.includes('/chat') || u.includes('/pipeline')) {
      console.log(`[http] ${resp.status()} ${u.replace(FRONTEND, '').slice(0, 70)}`)
    }
  })

  console.log(`[probe] 打开 ${FRONTEND}`)
  await page.goto(FRONTEND, { waitUntil: 'domcontentloaded', timeout: 60000 })

  // ---------- 登录 ----------
  console.log(`[probe] 登录 ${USERNAME}`)
  const userInput = page.locator('[data-testid="login-username-input"]')
  await userInput.waitFor({ state: 'visible', timeout: 30000 })
  const passInput = page.locator('[data-testid="login-password-input"]')
  await passInput.waitFor({ state: 'visible', timeout: 30000 })
  await userInput.click()
  await userInput.pressSequentially(USERNAME, { delay: 20 })
  await passInput.click()
  await passInput.pressSequentially(PASSWORD, { delay: 20 })
  await page.locator('button[type="submit"]').first().click()
  await page.waitForURL((url) => !url.pathname.includes('/login'), { timeout: 30000 })
  console.log(`[probe] 登录成功，URL: ${page.url()}`)
  await page.waitForTimeout(3000)

  // ---------- 进入已有会话 ----------
  const entered = await page.evaluate(() => {
    const sess = document.querySelector('[aria-label^="会话:"]')
    if (sess) {
      ;['pointerdown','mousedown','pointerup','mouseup','click'].forEach((t) =>
        sess.dispatchEvent(new MouseEvent(t, { bubbles: true, cancelable: true, view: window })))
      return 'existing'
    }
    return null
  })
  console.log(`[probe] 进入会话: ${entered}`)
  await page.waitForTimeout(3000)

  // ---------- 录制前快照 ----------
  const wsBeforeSend = wsStats.totalCount

  // ---------- 启动 CDP Tracing（完整火焰图）----------
  console.log('[probe] 开始 CDP Tracing 录制')
  await client.send('Tracing.start', {
    traceConfig: {
      includedCategories: [
        'devtools.timeline',
        'v8.execute',
        'disabled-by-default-v8.cpu_profiler',
        'disabled-by-default-devtools.timeline',
        'disabled-by-default-devtools.timeline.frame',
        'blink.user_timing',
        'toplevel',
      ],
      excludedCategories: ['*'],
    },
    transferMode: 'ReportEvents',
  })

  // ---------- 发消息 ----------
  const inputLocator = page.locator('[data-testid="chat-input-textarea"]').first()
  await inputLocator.waitFor({ state: 'visible', timeout: 30000 })
  await inputLocator.click()
  await inputLocator.fill(PROMPT)
  await page.waitForTimeout(500)
  console.log('[probe] === 按 Enter 发送 ===')
  await inputLocator.press('Enter')

  // ---------- 等 WS 流量增长 ----------
  let streamStarted = false
  for (let i = 0; i < 15; i++) {
    await page.waitForTimeout(1000)
    if (wsStats.totalCount > wsBeforeSend + 2) { streamStarted = true; break }
  }
  console.log(`[probe] 流式开始: ${streamStarted}（WS 接收: ${wsStats.totalCount}，发送前: ${wsBeforeSend}）`)
  if (!streamStarted) {
    // 截图看输入框状态 + dump 最近 HTTP
    await page.screenshot({ path: join(OUT_DIR, 'send-failed.png') })
    console.log('[probe] 最近 HTTP 请求:', httpReqs.slice(-8).map((r) => `${r.method} ${r.url}`).join('\n  '))
  }

  // ---------- 采样直到流式结束 ----------
  const samples = []
  const sampleStart = Date.now()
  let stableCount = 0
  let lastWsCount = wsStats.totalCount
  while (Date.now() - sampleStart < STREAM_WAIT_MS) {
    await page.waitForTimeout(2000)
    const snap = await page.evaluate(() => {
      const S = window.__probe
      const dur = (performance.now() - S.startedAt) / 1000
      const fps = S.frames / Math.max(0.1, dur)
      return { dur: dur.toFixed(1), fps: fps.toFixed(0), janky: S.jankyFrames, longTasks: S.longTasks.length }
    })
    const wsNow = wsStats.totalCount
    const wsDelta = wsNow - lastWsCount
    const topTypes = Object.entries(wsStats.byType).sort((a, b) => b[1] - a[1]).slice(0, 6).map(([k, v]) => `${k}=${v}`).join(' ')
    samples.push({ ...snap, ws: wsNow, wsDelta })
    console.log(`[probe ${snap.dur}s] WS=${wsNow}(+${wsDelta}) fps≈${snap.fps} janky=${snap.janky} longTasks=${snap.longTasks} | ${topTypes}`)
    lastWsCount = wsNow

    // 结束判定：WS 连续 2 次（4 秒）不增长 + 有 stream_end
    if (wsDelta === 0) {
      stableCount++
      if (stableCount >= 2 && (wsStats.byType['stream_end'] || wsStats.byType['new_message'])) {
        console.log('[probe] 流式结束')
        break
      }
    } else stableCount = 0
  }

  // ---------- 停止 trace 并等待数据回流 ----------
  console.log('[probe] 停止 CDP Tracing，等待数据...')
  await client.send('Tracing.end')
  await tracingCompletePromise
  const traceJson = JSON.stringify(traceEvents)
  const tracePath = join(OUT_DIR, 'jank-trace.json')
  writeFileSync(tracePath, traceJson)
  console.log(`[probe] trace 写入 ${tracePath} (${(traceJson.length / 1024).toFixed(0)}KB, ${traceEvents.length} events)`)

  // ---------- 汇总 ----------
  const pageSummary = await page.evaluate(() => {
    const S = window.__probe
    const dur = (performance.now() - S.startedAt) / 1000
    return {
      dur: dur.toFixed(1),
      totalFrames: S.frames,
      jankyFrames: S.jankyFrames,
      fps: (S.frames / Math.max(0.1, dur)).toFixed(1),
      longTaskCount: S.longTasks.length,
      longTaskBuckets: S.longTasks.reduce((acc, t) => {
        const b = t.t >= 500 ? '500ms+' : t.t >= 200 ? '200-500ms' : t.t >= 100 ? '100-200ms' : '30-100ms'
        acc[b] = (acc[b] || 0) + 1; return acc
      }, {}),
      topTasks: S.longTasks.slice().sort((a, b) => b.t - a.t).slice(0, 15),
    }
  })

  // WS payload 大小分布
  const sizes = wsStats.framesReceived.map((f) => f.size)
  const avgSize = sizes.length ? Math.round(sizes.reduce((a, b) => a + b, 0) / sizes.length) : 0
  const maxSize = sizes.length ? Math.max(...sizes) : 0
  // WS 接收速率（每秒帧数）
  const wsDuration = wsStats.framesReceived.length > 1
    ? wsStats.framesReceived[wsStats.framesReceived.length - 1].ts - wsStats.framesReceived[0].ts
    : 0

  const report = {
    config: { FRONTEND, USERNAME, HEADLESS, STREAM_WAIT_MS, PROMPT_LENGTH: PROMPT.length },
    streamStarted,
    ws: {
      totalCount: wsStats.totalCount,
      totalBytes: wsStats.totalBytes,
      avgPayloadBytes: avgSize,
      maxPayloadBytes: maxSize,
      durationSec: wsDuration.toFixed(2),
      framesPerSec: wsDuration > 0 ? (wsStats.totalCount / wsDuration).toFixed(1) : '0',
      byType: wsStats.byType,
    },
    page: pageSummary,
    httpRequests: httpReqs.slice(-20),
    samples,
  }
  writeFileSync(join(OUT_DIR, 'jank-report.json'), JSON.stringify(report, null, 2))

  console.log('\n========== 卡顿诊断汇总 ==========')
  console.log(`流式触发: ${streamStarted ? '✓ 成功' : '✗ 失败'}`)
  console.log(`WS 接收: ${wsStats.totalCount} 帧, ${(wsStats.totalBytes / 1024).toFixed(1)}KB, 平均 ${avgSize}B/帧, 峰值 ${maxSize}B`)
  console.log(`WS 速率: ${report.ws.framesPerSec} 帧/秒`)
  console.log(`WS 类型分布:`, wsStats.byType)
  console.log(`帧率: ${pageSummary.fps} fps, 丢帧(>25ms): ${pageSummary.jankyFrames}/${pageSummary.totalFrames}`)
  console.log(`长任务(>30ms): ${pageSummary.longTaskCount} 次`, pageSummary.longTaskBuckets)
  console.log(`Top 任务:`)
  pageSummary.topTasks.slice(0, 8).forEach((t, i) => console.log(`  ${i + 1}. ${t.t}ms  ${t.name}  @${t.at}ms`))
  console.log(`\ntrace: ${tracePath}`)
  console.log(`报告: ${join(OUT_DIR, 'jank-report.json')}`)

  await browser.close()
}

main().catch((e) => {
  console.error('[probe] 失败:', e)
  process.exit(1)
})
