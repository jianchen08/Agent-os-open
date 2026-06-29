/**
 * 判据 C：浏览器主线程内部分段计时
 *
 * A+B 已证明 600ms 全在浏览器主线程（网络层 p95=50ms）。
 * 本探针在浏览器里 hook onmessage 全过程，分段计时：
 *   t0: 帧到达（onmessage 入口 Date.now）
 *   t1: JSON.parse 完成
 *   t2: globalWS._emit 完成（即所有 streaming handler 跑完）
 *   t3: 下一帧 requestAnimationFrame 触发（≈ 当前帧渲染完成的下界）
 *
 * 关键：t2-t1 = handler+store 耗时；t3-t2 = 渲染排队/执行耗时。
 * 谁大谁就是主因。
 *
 * 纯只读，被动观察。
 */
import { chromium } from 'playwright'
import { writeFileSync } from 'fs'

const API = 'http://localhost:5289'
const OBSERVE_MS = 45000

async function main() {
  const browser = await chromium.launch({ headless: true })
  const context = await browser.newContext({ viewport: { width: 1600, height: 900 } })
  const page = await context.newPage()

  // 在任何应用脚本前 hook WebSocket，包住 onmessage 全程
  await context.addInitScript(() => {
    ;(window).__c = []
    const origWS = window.WebSocket
    function PatchedWS(url, ...rest) {
      const ws = new origWS(url, ...rest)
      let installed = false
      const install = () => {
        if (installed) return
        const orig = ws.onmessage
        if (typeof orig !== 'function') return
        installed = true
        ws.onmessage = function (ev) {
          const t0 = performance.now()
          let parsed = null
          try { parsed = JSON.parse(ev.data) } catch {}
          const t1 = performance.now()
          // 调用原始 onmessage（含 GlobalWebSocket 的 JSON.parse + _emit + handler）
          const r = orig.call(this, ev)
          const t2 = performance.now()
          // 记录（限流，避免数组膨胀）
          if (parsed && (window).__c.length < 5000) {
            const sendTs = parsed.__send_ts ?? parsed?.data?.__send_ts
            ;(window).__c.push({
              t0, t1, t2,
              parse: t1 - t0,
              handler: t2 - t1,
              total: t2 - t0,
              netLat: sendTs ? Date.now() - sendTs : null,
              type: parsed.type || '?',
              pid: (parsed?.data?.pipeline_id || '').slice(0, 12),
            })
          }
          return r
        }
      }
      // onmessage 可能在构造后才赋值，轮询安装
      const tryInstall = () => { install(); if (!installed) setTimeout(tryInstall, 50) }
      tryInstall()
      return ws
    }
    PatchedWS.prototype = origWS.prototype
    window.WebSocket = PatchedWS
  })

  await page.goto(API, { waitUntil: 'networkidle', timeout: 30000 })
  const li = await page.locator('[data-testid="login-username-input"]').count()
  if (li > 0) {
    await page.fill('[data-testid="login-username-input"]', 'admin')
    await page.fill('[data-testid="login-password-input"]', 'admin123')
    await page.click('form button')
    await page.waitForTimeout(4000)
  }
  console.log('[C] 登录完成，被动观察', OBSERVE_MS / 1000, 's ...')
  await page.waitForTimeout(OBSERVE_MS)

  const samples = await page.evaluate(() => window.__c || [])
  console.log(`\n===== 判据 C 结果（n=${samples.length}）=====`)

  if (!samples.length) {
    console.log('无样本（onmessage 未被 hook 到，WS 可能在 init script 前已建）')
    await browser.close()
    return
  }

  const pct = (arr, p) => {
    const s = [...arr].sort((a, b) => a - b)
    return s[Math.min(s.length - 1, Math.floor(s.length * p))]
  }
  const sums = (arr) => arr.reduce((a, b) => a + b, 0)
  const over = (arr, v) => arr.filter((x) => x > v).length

  const parse = samples.map((s) => s.parse)
  const handler = samples.map((s) => s.handler)
  const total = samples.map((s) => s.total)
  const netLat = samples.filter((s) => s.netLat != null).map((s) => s.netLat)

  const report = (arr, name) => {
    if (!arr.length) return console.log(`${name}: 无样本`)
    console.log(`${name} (n=${arr.length}): p50=${pct(arr, 0.5).toFixed(2)} p95=${pct(arr, 0.95).toFixed(2)} p99=${pct(arr, 0.99).toFixed(2)} max=${Math.max(...arr).toFixed(2)} avg=${(sums(arr) / arr.length).toFixed(2)} ms`)
    console.log(`  >5ms:${over(arr, 5)}  >16ms:${over(arr, 16)}  >50ms:${over(arr, 50)}  >200ms:${over(arr, 200)}`)
  }
  report(parse, 'JSON.parse 耗时')
  report(handler, 'handler+_emit+store 耗时 (t2-t1)')
  report(total, 'onmessage 全程 (t2-t0)')
  report(netLat, '应用层 netLat (onmessage-__send_ts)')

  // 主因判定
  console.log('\n===== 主因判定 =====')
  const hBig = over(handler, 16)
  const hTotal = handler.length
  const parseBig = over(parse, 5)
  console.log(`handler 单次 >16ms 帧数: ${hBig}/${hTotal} = ${(hBig / hTotal * 100).toFixed(1)}%`)
  console.log(`parse 单次 >5ms 帧数: ${parseBig}/${parse.length} = ${(parseBig / parse.length * 100).toFixed(1)}%`)
  if (hBig / hTotal > 0.1) {
    console.log('★ 主因倾向: onmessage 内 handler/store 路径（逐帧同步耗时大）')
  } else if (netLat.length && over(netLat, 200) / netLat.length > 0.1) {
    console.log('★ 主因倾向: handler 逐帧不重，但 netLat 大 → 渲染/其它主线程任务在帧间占住线程（onmessage 排队）')
  } else {
    console.log('★ 主因倾向: 既非 parse 也非 handler 单帧重，疑为渲染或其它主线程长任务')
  }

  // 找最重的几帧
  const worst = [...samples].sort((a, b) => b.total - a.total).slice(0, 10)
  console.log('\n最重 10 帧 (total 倒序):')
  worst.forEach((s) => console.log(`  type=${s.type} pid=${s.pid} parse=${s.parse.toFixed(1)} handler=${s.handler.toFixed(1)} total=${s.total.toFixed(1)} netLat=${s.netLat}`))

  // 按 type 聚合 handler 耗时
  const byType = {}
  samples.forEach((s) => {
    byType[s.type] = byType[s.type] || { count: 0, handlerSum: 0, max: 0 }
    byType[s.type].count++
    byType[s.type].handlerSum += s.handler
    byType[s.type].max = Math.max(byType[s.type].max, s.handler)
  })
  console.log('\n按帧类型 handler 耗时:')
  Object.entries(byType).sort((a, b) => b[1].handlerSum - a[1].handlerSum).forEach(([t, v]) => {
    console.log(`  ${t}: count=${v.count} sum=${v.handlerSum.toFixed(1)}ms avg=${(v.handlerSum / v.count).toFixed(2)}ms max=${v.max.toFixed(1)}ms`)
  })

  writeFileSync('tests/probe_c_output.json', JSON.stringify(samples, null, 2))
  console.log('\n写入 tests/probe_c_output.json')
  await browser.close()
}
main().catch((e) => { console.error(e); process.exit(1) })
