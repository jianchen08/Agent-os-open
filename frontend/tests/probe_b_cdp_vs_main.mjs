/**
 * 判据 B：浏览器 CDP 网络层 vs 应用层 onmessage 延迟对照
 *
 * 判据 A 已证明后端→Python客户端 p95=5.5ms（后端/网络清白）。
 * 本探针在真实浏览器里同时打两个时间戳：
 *   - CDP webSocketFrameReceived：网络层，帧到达 socket 即触发（不经过主线程）
 *   - 应用层 onmessage：必须等主线程空闲才执行
 * 二者之差 = 纯浏览器主线程排队延迟。
 *
 * 纯只读：不点新会话、不发消息、不触发 LLM。只登录后被动观察当前页面的 WS 流。
 */
import { chromium } from 'playwright'
import { writeFileSync } from 'fs'

const API = 'http://localhost:5289'
const WS_FRAMES = []
const OBSERVE_MS = 45000

async function main() {
  const browser = await chromium.launch({ headless: true })
  const context = await browser.newContext({ viewport: { width: 1600, height: 900 } })
  const page = await context.newPage()

  // CDP 拦截：网络层帧到达（不经过主线程）
  const client = await context.newCDPSession(page)
  await client.send('Network.enable')
  await client.on('Network.webSocketFrameReceived', (e) => {
    try {
      const d = JSON.parse(e.response.payloadData)
      const sendTs = d.__send_ts ?? d?.data?.__send_ts
      const typ = d.type || '?'
      const pid = (d?.data?.pipeline_id || '').slice(0, 12)
      const t = Date.now()
      // 把帧信息挂到 window 上（CDP 回调里 page.evaluate 太重，改用数组暂存）
      WS_FRAMES.push({ cdp_recv: t, type: typ, pid, sendTs })
    } catch {}
  })

  // 登录
  await page.goto(API, { waitUntil: 'networkidle', timeout: 30000 })
  const li = await page.locator('[data-testid="login-username-input"]').count()
  if (li > 0) {
    await page.fill('[data-testid="login-username-input"]', 'admin')
    await page.fill('[data-testid="login-password-input"]', 'admin123')
    await page.click('form button')
    await page.waitForTimeout(4000)
  }
  console.log('[B] 登录完成，等待 WS 稳定...')
  await page.waitForTimeout(3000)

  // 在页面里注入应用层 onmessage 探针：patch 已存在的 WebSocket 实例的监听器
  // 更稳的方式：用 performance API 拦不到。改为 page.exposeFunction 让 onmessage 回传。
  const appRecv = []
  await page.addInitScript(() => {
    // 包装 onmessage setter：GlobalWebSocket 用 ws.onmessage = ...
    const desc = Object.getOwnPropertyDescriptor(WebSocket.prototype, 'onmessage')
    // 由于 WS 已建立，直接 hook 不可行。改为轮询从 window.__appFrames 读取
    ;(window).__appFrames = []
    const origWS = window.WebSocket
    function PatchedWS(url, ...rest) {
      const ws = new origWS(url, ...rest)
      ws.addEventListener('message', (ev) => {
        try {
          const d = JSON.parse(ev.data)
          const sendTs = d.__send_ts ?? d?.data?.__send_ts
          ;(window).__appFrames.push({
            app_recv: Date.now(),
            type: d.type || '?',
            pid: (d?.data?.pipeline_id || '').slice(0, 12),
            sendTs,
          })
        } catch {}
      })
      return ws
    }
    PatchedWS.prototype = origWS.prototype
    window.WebSocket = PatchedWS
    // 但 WS 可能在本 init script 之前已创建。尝试 re-fetch 已连接实例不可行。
    // 因此 addInitScript 的 hook 只能拦后续新建的 WS。
  })

  // 等待，期间 CDP 持续收集网络层帧
  console.log(`[B] 被动观察 ${OBSERVE_MS / 1000}s ...`)
  await page.waitForTimeout(OBSERVE_MS)

  // 读取应用层帧
  const appFrames = await page.evaluate(() => window.__appFrames || [])

  console.log(`\n===== 判据 B 结果 =====`)
  console.log(`CDP 网络层帧数: ${WS_FRAMES.length}`)
  console.log(`应用层 onmessage 帧数: ${appFrames.length}`)

  // CDP 层延迟：网络层收到 - __send_ts（≈ 判据A的 Python 口径）
  const cdpLats = WS_FRAMES.filter((f) => f.sendTs).map((f) => f.cdp_recv - f.sendTs)
  // 应用层延迟：onmessage 执行 - __send_ts
  const appLats = appFrames.filter((f) => f.sendTs).map((f) => f.app_recv - f.sendTs)

  const stat = (arr, name) => {
    if (!arr.length) return console.log(`${name}: 无样本`)
    arr.sort((a, b) => a - b)
    const n = arr.length
    console.log(`${name} (n=${n}): min=${arr[0]} p50=${arr[n >> 1]} p95=${arr[int(n * 0.95)]} p99=${arr[Math.min(n - 1, int(n * 0.99))]} max=${arr[n - 1]} avg=${Math.round(arr.reduce((a, b) => a + b, 0) / n)}`)
    console.log(`  >100ms:${arr.filter((x) => x > 100).length}  >200ms:${arr.filter((x) => x > 200).length}  >500ms:${arr.filter((x) => x > 500).length}`)
  }
  function int(x) { return Math.min(arr_len(), Math.floor(x)) }
  function arr_len() { return Infinity }

  // 重写 stat 避免 int 闭包问题
  const stat2 = (arr, name) => {
    if (!arr.length) return console.log(`${name}: 无样本`)
    arr.sort((a, b) => a - b)
    const n = arr.length
    const at = (p) => arr[Math.min(n - 1, Math.floor(p))]
    const avg = Math.round(arr.reduce((a, b) => a + b, 0) / n)
    console.log(`${name} (n=${n}): min=${arr[0]} p50=${at(n / 2)} p95=${at(n * 0.95)} p99=${at(n * 0.99)} max=${arr[n - 1]} avg=${avg}`)
    console.log(`  >100ms:${arr.filter((x) => x > 100).length}  >200ms:${arr.filter((x) => x > 200).length}  >500ms:${arr.filter((x) => x > 500).length}`)
  }
  stat2(cdpLats, '\nCDP网络层延迟(浏览器收到-后端发出)')
  stat2(appLats, '应用层延迟(onmessage执行-后端发出)')

  // 主线程排队 = 应用层 - 网络层（逐帧配对，用 sendTs 作 key 近似）
  if (cdpLats.length && appLats.length) {
    console.log(`\n★ 主线程排队延迟 ≈ 应用层延迟 - 网络层延迟`)
    console.log(`  网络层 p95=${cdpLats.sort((a,b)=>a-b)[Math.floor(cdpLats.length*0.95)]}ms  vs  应用层 p95=${appLats.sort((a,b)=>a-b)[Math.floor(appLats.length*0.95)]}ms`)
  }

  // 帧分布
  const dist = {}
  WS_FRAMES.forEach((f) => { dist[f.pid || '(空)'] = (dist[f.pid || '(空)'] || 0) + 1 })
  console.log('\nCDP 帧按 pipeline 分布(前8):')
  Object.entries(dist).sort((a, b) => b[1] - a[1]).slice(0, 8).forEach(([p, c]) => console.log(`  ${p}: ${c}`))

  writeFileSync('tests/probe_b_output.json', JSON.stringify({
    cdpFrames: WS_FRAMES.length,
    appFrames: appFrames.length,
    cdpLats, appLats,
  }, null, 2))
  console.log('\n写入 tests/probe_b_output.json')
  await browser.close()
}
main().catch((e) => { console.error(e); process.exit(1) })
