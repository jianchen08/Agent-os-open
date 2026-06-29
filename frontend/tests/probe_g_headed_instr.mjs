/**
 * 判据 G（需在你的环境跑）：headed 浏览器 + 真实 profile 抓 long task + netLat
 *
 * 用法（在前端目录）：
 *   node tests/probe_g_headed_instr.mjs
 *
 * 它会：
 *   1. 用你真实的浏览器 profile（复用 localStorage/persist，复现"持续卡"的前提）
 *   2. headed（非 headless，有真实渲染，复现首帧渲染阻塞）
 *   3. 进入后被动观察 40s，抓：
 *      - long task 列表 + 调用栈（Profiler）
 *      - 应用层 netLat 分布（对比判据A/B的50ms，看是否飙到 600ms）
 *      - persist 的 activePipelineId vs 正在推流的 pid
 *   4. 导出 cpuprofile，可拖进 Chrome DevTools Performance 看
 *
 * 关键：你要在出现的浏览器窗口里【手动切到有历史消息的会话】，
 *       触发"首次加载"场景，这样能抓到首帧渲染的 long task。
 */
import { chromium } from 'playwright'
import { writeFileSync } from 'fs'

const API = 'http://localhost:5289'

async function main() {
  // ★ headed + 用真实 profile 复现
  const browser = await chromium.launch({
    headless: false,
    // 复用你的登录态/persist：指向你的 Chrome user-data-dir
    // 如不生效，可改为绝对路径，例如：
    //   userDataDir: 'C:/Users/jc/AppData/Local/Google/Chrome/User Data/Default'
    args: ['--start-maximized'],
  })
  const context = await browser.newContext({ viewport: null })
  const page = await context.newPage()

  const client = await context.newCDPSession(page)
  await client.send('Profiler.enable')
  await client.send('Network.enable')

  const pushPids = {}
  client.on('Network.webSocketFrameReceived', (e) => {
    try {
      const d = JSON.parse(e.response.payloadData)
      const pid = d?.data?.pipeline_id || ''
      if (pid) pushPids[pid] = (pushPids[pid] || 0) + 1
    } catch {}
  })

  // 应用层 netLat 探针
  await context.addInitScript(() => {
    ;(window).__net = []
    ;(window).__long = []
    const origWS = window.WebSocket
    function PWS(url, ...r) {
      const ws = new origWS(url, ...r)
      let done = false
      const wrap = () => {
        if (done) return
        const o = ws.onmessage
        if (typeof o !== 'function') return
        done = true
        ws.onmessage = function (ev) {
          try {
            const d = JSON.parse(ev.data)
            const st = d.__send_ts ?? d?.data?.__send_ts
            if (st && (window).__net.length < 8000) {
              ;(window).__net.push({ lat: Date.now() - st, type: d.type, pid: (d?.data?.pipeline_id || '').slice(0, 12) })
            }
          } catch {}
          return o.call(this, ev)
        }
      }
      const t = () => { wrap(); if (!done) setTimeout(t, 30) }
      t()
      return ws
    }
    PWS.prototype = origWS.prototype
    window.WebSocket = PWS
    try {
      new PerformanceObserver((l) => {
        for (const en of l.getEntries()) if ((window).__long.length < 1000) (window).__long.push({ dur: en.duration, start: en.startTime })
      }).observe({ entryTypes: ['longtask'] })
    } catch {}
  })

  await page.goto(API, { waitUntil: 'domcontentloaded', timeout: 30000 })
  console.log('[G] 页面已打开。如需登录请手动登录，然后【手动点开一个有历史消息的会话】触发首次加载。')
  console.log('[G] 准备好后，20秒后自动开始抓取。先让你操作 ...')
  await page.waitForTimeout(20000)

  // 读 persist
  const persisted = await page.evaluate(() => {
    try {
      const raw = localStorage.getItem('pipeline-messages')
      if (!raw) return { exists: false }
      const o = JSON.parse(raw)
      const s = o.state || o
      return { exists: true, active: (s.activePipelineId || '').slice(0, 12), msgs: Object.keys(s.messagesByPipeline || {}).map((k) => k.slice(0, 12)) }
    } catch (e) { return { err: String(e) } }
  })

  await client.send('Profiler.start')
  console.log('[G] 开始抓取 40s（期间可继续切换会话/前后台切换）...')
  await page.waitForTimeout(40000)
  const { profile } = await client.send('Profiler.stop')

  const data = await page.evaluate(() => {
    const net = window.__net || []
    const long = window.__long || []
    const lats = net.map((n) => n.lat).sort((a, b) => a - b)
    const p = (arr, q) => arr[Math.min(arr.length - 1, Math.floor(arr.length * q))]
    return {
      netCount: net.length,
      netP50: lats.length ? p(lats, 0.5) : null,
      netP95: lats.length ? p(lats, 0.95) : null,
      netMax: lats.length ? lats[lats.length - 1] : null,
      netOver200: lats.filter((x) => x > 200).length,
      netOver500: lats.filter((x) => x > 500).length,
      longCount: long.length,
      longMax: long.length ? Math.max(...long.map((l) => l.dur)) : 0,
      longs: long,
    }
  })

  console.log('\n========== 判据 G 结果 ==========')
  console.log('persist:', JSON.stringify(persisted))
  console.log(`\n应用层 netLat (n=${data.netCount}): p50=${data.netP50} p95=${data.netP95} max=${data.netMax}  >200ms:${data.netOver200}  >500ms:${data.netOver500}`)
  console.log(`long task: ${data.longCount} 个, 最大 ${data.longMax.toFixed(0)}ms`)
  console.log('long task 明细:')
  data.longs.slice(0, 20).forEach((l) => console.log(`  ${l.dur.toFixed(0)}ms @+${(l.start / 1000).toFixed(1)}s`))

  console.log('\n推流 pid:')
  Object.entries(pushPids).sort((a, b) => b[1] - a[1]).slice(0, 6).forEach(([p, c]) => console.log(`  ${p.slice(0, 12)}: ${c}`))
  if (persisted.exists && persisted.active) {
    const inPush = pushPids[persisted.active] || 0
    console.log(`\n★ persist active=${persisted.active} 是否在推流: ${inPush > 0 ? '是(' + inPush + '帧) ★★ 可能是根因' : '否'}`)
  }

  console.log(`\n对比：判据A(Python)=p95 5ms, 判据B(CDP网络层)=p95 50ms`)
  console.log(`     若本测 netMax 远大于 50ms → 主线程被占坐实，看 long task 对应时间段`)

  writeFileSync('tests/probe_g_output.json', JSON.stringify({ persisted, data, pushPids }, null, 2))
  writeFileSync('tests/probe_g_profile.cpuprofile', JSON.stringify(profile))
  console.log('\n写入 tests/probe_g_output.json 和 tests/probe_g_profile.cpuprofile')
  console.log('cpuprofile 可拖进 Chrome → DevTools → Performance 查看调用栈')
  await browser.close()
}
main().catch((e) => { console.error(e); process.exit(1) })
