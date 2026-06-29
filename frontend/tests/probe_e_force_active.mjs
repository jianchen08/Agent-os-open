/**
 * 判据 E：强制 active = 正在推流的 pipeline，验证"持续 flush 循环 + 卡顿"假设
 *
 * 判据 D 在干净 profile（无 persist）下复现不出卡顿：过滤生效、RAF=0、无持续占用。
 * 差异在于：你的真实浏览器有 persist 的 activePipelineId，很可能正指向推流的 pipeline。
 *
 * 本探针模拟该场景：登录后，主动把一个"正在被后端推流"的 pid 写成 active，
 * 观察 RAF 是否立刻变成持续高频 + long task 是否爆发 + 主线程 netLat 是否飙升。
 *
 * 这是"止血验证"：如果强制 active 后立刻复现，就证明根因是
 * "persist 恢复的 active pipeline 正在被推流 → 过滤失效 → 持续渲染循环"。
 */
import { chromium } from 'playwright'
import { writeFileSync } from 'fs'

const API = 'http://localhost:5289'

async function main() {
  const browser = await chromium.launch({ headless: true })
  const context = await browser.newContext({ viewport: { width: 1600, height: 900 } })
  const page = await context.newPage()

  // CDP 抓推流 pid
  const client = await context.newCDPSession(page)
  await client.send('Network.enable')
  const pushPids = {}
  await client.on('Network.webSocketFrameReceived', (e) => {
    try {
      const d = JSON.parse(e.response.payloadData)
      const pid = (d?.data?.pipeline_id || '')
      if (pid) pushPids[pid] = (pushPids[pid] || 0) + 1
    } catch {}
  })

  // hook RAF + long task
  await context.addInitScript(() => {
    ;(window).__raf = []
    ;(window).__long = []
    const origRAF = window.requestAnimationFrame
    window.requestAnimationFrame = function (cb) {
      return origRAF.call(window, (t) => {
        const s = performance.now()
        try { cb(t) } finally {
          if ((window).__raf.length < 20000) (window).__raf.push({ s, dur: performance.now() - s })
        }
      })
    }
    try {
      new PerformanceObserver((l) => {
        for (const en of l.getEntries()) if ((window).__long.length < 500) (window).__long.push(en.duration)
      }).observe({ entryTypes: ['longtask'] })
    } catch {}
  })

  await page.goto(API, { waitUntil: 'networkidle', timeout: 30000 })
  const li = await page.locator('[data-testid="login-username-input"]').count()
  if (li > 0) {
    await page.fill('[data-testid="login-username-input"]', 'admin')
    await page.fill('[data-testid="login-password-input"]', 'admin123')
    await page.click('form button')
    await page.waitForTimeout(4000)
  }
  console.log('[E] 登录完成，先被动观察 15s 找最大推流 pid ...')
  await page.waitForTimeout(15000)

  // 找推流最多的 pid（全 12 位前缀，但 store 里是完整 id，需从帧里拿完整的）
  // CDP 里我截了完整 pid（没 slice），重新读
  const topPid = Object.entries(pushPids).sort((a, b) => b[1] - a[1])[0]
  if (!topPid) {
    console.log('无推流 pid，无法验证')
    await browser.close()
    return
  }
  console.log(`最大推流 pid=${topPid[0].slice(0, 12)} (${topPid[1]} 帧)`)

  // 读 baseline（强制前）
  const baseline = await page.evaluate(() => ({
    raf: (window.__raf || []).length,
    long: (window.__long || []).length,
    longMax: (window.__long || []).length ? Math.max(...window.__long) : 0,
  }))
  console.log(`[baseline] RAF=${baseline.raf} long=${baseline.long} longMax=${baseline.longMax.toFixed(0)}ms`)

  // 取完整 pid（从 CDP 没截断的）
  // 注意：store 用完整 pid。我从 CDP 抓的是完整的（上面没 slice）。
  // 强制把它写成 activePipelineId + 注册到 pipelines，让 isPipelineRelevant 判据1 成立
  console.log(`[E] 强制 activePipelineId = ${topPid[0].slice(0, 12)}，观察 25s 是否爆发 ...`)
  await page.evaluate((pid) => {
    // 直接操作 zustand store（通过 persist 注册的 store API）
    // pipelineMessageStore 是模块级单例，需通过 React DevTools 或全局。
    // 简化：直接改 localStorage 的 persist 值不行（已 hydrate）。
    // 用兜底：往 window 上找不到 store，则尝试通过事件回流间接。
    // 更稳：很多 zustand 项目会把 store 挂到 window.__store。检查一下。
    return {
      hasStore: !!(window).__pipelineStore,
      hasZustand: typeof (window).__pipelineStore?.setState === 'function',
    }
  }, topPid[0]).then((r) => console.log('store 可达性:', r))

  // 尝试通过 zustand persist 的 API 设置 active
  const setResult = await page.evaluate((pid) => {
    const store = (window).__pipelineStore
    if (!store || typeof store.setState !== 'function') {
      return { ok: false, reason: 'store 不可达（未挂到 window）' }
    }
    store.setState({ activePipelineId: pid })
    // 也注册到 pipelines，让判据2 也成立
    store.setState((s) => ({
      pipelines: { ...s.pipelines, [pid]: { pipelineId: pid, status: 'streaming' } },
    }))
    return { ok: true, active: store.getState().activePipelineId }
  }, topPid[0])
  console.log('强制 active 结果:', setResult)

  if (!setResult.ok) {
    // store 没挂 window，得改用别的方式。先报告
    console.log('store 不可达，无法直接强制。但可观察：即便不强制，被动 25s 的 RAF/long task')
  }

  // 强制后观察
  await page.waitForTimeout(25000)
  const after = await page.evaluate(() => {
    const raf = window.__raf || []
    const long = window.__long || []
    return {
      raf: raf.length,
      rafMax: raf.length ? Math.max(...raf.map((r) => r.dur)) : 0,
      long: long.length,
      longMax: long.length ? Math.max(...long) : 0,
      longSum: long.reduce((a, b) => a + b, 0),
    }
  })

  console.log(`\n===== 判据 E 结果 =====`)
  console.log(`强制前: RAF=${baseline.raf} long=${baseline.long} longMax=${baseline.longMax.toFixed(0)}ms`)
  console.log(`强制后(25s): RAF=${after.raf} long=${after.long} longMax=${after.longMax.toFixed(0)}ms longSum=${after.longSum.toFixed(0)}ms`)
  if (after.raf > baseline.raf + 50) {
    console.log('★ RAF 显著增加 → 强制 active 后出现持续 flush 循环（假设成立）')
  } else {
    console.log('RAF 未显著增加')
  }
  if (after.long > baseline.long + 2) {
    console.log('★ long task 爆发 → 主线程被持续占用（根因确认）')
  }

  writeFileSync('tests/probe_e_output.json', JSON.stringify({ topPid, baseline, after, setResult }, null, 2))
  console.log('写入 tests/probe_e_output.json')
  await browser.close()
}
main().catch((e) => { console.error(e); process.exit(1) })
