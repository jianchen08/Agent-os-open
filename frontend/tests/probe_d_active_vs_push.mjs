/**
 * 判据 D：persist 恢复的 activePipelineId 是否正是持续推流的僵尸 pipeline
 *
 * 假设：persist 恢复 activePipelineId = 某个后端持续推流的 pipeline，
 *   导致 isPipelineRelevant 判据1 成立 → 它的 chunk 不被过滤 → 进 RAF buffer
 *   → 持续 RAF flush + setState + React 重渲染 → 主线程持续被占。
 *   后台 RAF 停 → 积压 → 切回爆发（解释"后台切回也卡"）。
 *
 * 本探针验证：
 *   1. 登录后 persist 恢复的 activePipelineId 是什么
 *   2. 它是否正在被后端推流（对比判据C的推流 pid）
 *   3. RAF 稳态调度频率（持续 flush 循环存在的证据）
 *   4. long task（主线程被占的直接证据）
 *
 * 纯只读，被动观察，不发消息。
 */
import { chromium } from 'playwright'
import { writeFileSync } from 'fs'

const API = 'http://localhost:5289'
const OBSERVE_MS = 35000

async function main() {
  const browser = await chromium.launch({ headless: true })
  const context = await browser.newContext({ viewport: { width: 1600, height: 900 } })
  const page = await context.newPage()

  // CDP 抓网络层帧，确认哪些 pipeline 在推流
  const client = await context.newCDPSession(page)
  await client.send('Network.enable')
  const pushPids = {}
  await client.on('Network.webSocketFrameReceived', (e) => {
    try {
      const d = JSON.parse(e.response.payloadData)
      const pid = (d?.data?.pipeline_id || '').slice(0, 12)
      if (pid) pushPids[pid] = (pushPids[pid] || 0) + 1
    } catch {}
  })

  // 全局 hook RAF + long task（在页面脚本前注入）
  await context.addInitScript(() => {
    ;(window).__raf = []
    ;(window).__long = []
    const origRAF = window.requestAnimationFrame
    window.requestAnimationFrame = function (cb) {
      return origRAF.call(window, (t) => {
        const s = performance.now()
        let r
        try { r = cb(t) } finally {
          const e = performance.now()
          if ((window).__raf.length < 8000) {
            ;(window).__raf.push({ s, e, dur: e - s })
          }
        }
        return r
      })
    }
    try {
      const po = new PerformanceObserver((list) => {
        for (const entry of list.getEntries()) {
          if ((window).__long.length < 500) {
            ;(window).__long.push({ start: entry.startTime, dur: entry.duration })
          }
        }
      })
      po.observe({ entryTypes: ['longtask'] })
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
  console.log('[D] 登录完成')

  // 读 persist 恢复的状态
  const persisted = await page.evaluate(() => {
    try {
      const raw = localStorage.getItem('pipeline-messages')
      if (!raw) return { exists: false }
      const obj = JSON.parse(raw)
      const state = obj.state || obj
      return {
        exists: true,
        activePipelineId: (state.activePipelineId || '').slice(0, 12),
        pipelineKeys: Object.keys(state.pipelines || {}).map((k) => k.slice(0, 12)),
        msgPipelineKeys: Object.keys(state.messagesByPipeline || {}).map((k) => k.slice(0, 12)),
        msgCounts: Object.fromEntries(
          Object.entries(state.messagesByPipeline || {})
            .map(([k, v]) => [k.slice(0, 12), Array.isArray(v) ? v.length : 0])
        ),
      }
    } catch (e) {
      return { error: String(e) }
    }
  })

  console.log('\n===== 判据 D-1：persist 恢复的 active pipeline =====')
  console.log('persist 存在:', persisted.exists)
  if (persisted.exists) {
    console.log('★ activePipelineId:', JSON.stringify(persisted.activePipelineId))
    console.log('已注册 pipelines:', JSON.stringify(persisted.pipelineKeys))
    console.log('有消息的 pipeline:', JSON.stringify(persisted.msgCounts))
  } else if (persisted.error) {
    console.log('读取错误:', persisted.error)
  } else {
    console.log('（首次登录，无 persist）')
  }

  // 被动观察，收 RAF 频率 + long task + 推流 pid
  console.log(`\n[D] 被动观察 ${OBSERVE_MS / 1000}s ...`)
  await page.waitForTimeout(OBSERVE_MS)

  const probe = await page.evaluate(() => {
    const raf = window.__raf || []
    const long = window.__long || []
    // RAF 频率：按 1s 桶统计
    if (!raf.length) return { rafCount: 0, longCount: long.length, longs: long }
    const t0 = raf[0].s
    const buckets = {}
    for (const r of raf) {
      const sec = Math.floor((r.s - t0) / 1000)
      buckets[sec] = buckets[sec] || { count: 0, durSum: 0, max: 0 }
      buckets[sec].count++
      buckets[sec].durSum += r.dur
      buckets[sec].max = Math.max(buckets[sec].max, r.dur)
    }
    const rafDurs = raf.map((r) => r.dur).sort((a, b) => a - b)
    return {
      rafCount: raf.length,
      rafBySec: Object.entries(buckets).map(([s, v]) => ({
        sec: +s,
        count: v.count,
        avgDur: +(v.durSum / v.count).toFixed(2),
        maxDur: +v.max.toFixed(2),
      })),
      rafP50: +rafDurs[rafDurs.length >> 1].toFixed(2),
      rafP95: +rafDurs[Math.min(rafDurs.length - 1, rafDurs.length * 0.95) | 0].toFixed(2),
      rafMax: +rafDurs[rafDurs.length - 1].toFixed(2),
      longCount: long.length,
      longs: long.slice(0, 30),
    }
  })

  console.log('\n===== 判据 D-2：稳态 RAF 频率（持续 flush 循环的证据）=====')
  console.log(`RAF 总回调数: ${probe.rafCount}（${OBSERVE_MS / 1000}s 内）`)
  if (probe.rafCount > 0) {
    console.log(`RAF 回调耗时(ms): p50=${probe.rafP50} p95=${probe.rafP95} max=${probe.rafMax}`)
    console.log('按秒桶（count=该秒RAF触发次数，正常空闲应≈0或低频）:')
    probe.rafBySec.forEach((b) => console.log(`  +${b.sec}s: ${b.count}次  avg=${b.avgDur}ms max=${b.maxDur}ms`))
    const totalPerSec = probe.rafCount / (OBSERVE_MS / 1000)
    console.log(`\n平均 ${totalPerSec.toFixed(1)} 次/秒`)
    if (totalPerSec > 15) console.log('★ RAF 持续高频触发 → 存在持续 flush+渲染循环')
    else console.log('RAF 低频 → 无持续 flush 循环')
  }

  console.log('\n===== 判据 D-3：long task（主线程被占 >50ms）=====')
  console.log(`long task 数: ${probe.longCount}`)
  if (probe.longCount > 0) {
    const durs = probe.longs.map((l) => l.dur).sort((a, b) => b - a)
    console.log(`最大: ${durs[0].toFixed(0)}ms  前5: ${durs.slice(0, 5).map((d) => d.toFixed(0)).join(', ')}`)
  }

  console.log('\n===== 判据 D-4：正在推流的 pipeline =====')
  const sorted = Object.entries(pushPids).sort((a, b) => b[1] - a[1])
  sorted.slice(0, 8).forEach(([p, c]) => console.log(`  ${p}: ${c} 帧`))

  // 交叉比对
  console.log('\n===== 判据 D-5：active pipeline 是否正在被推流 =====')
  if (persisted.exists && persisted.activePipelineId) {
    const active = persisted.activePipelineId
    const pushing = pushPids[active] || 0
    console.log(`activePipelineId=${active} 推流帧数=${pushing}`)
    if (pushing > 0) {
      console.log('★★★ 确认：persist 恢复的 active pipeline 正在被后端持续推流')
      console.log('       → 它的 chunk 不被 isPipelineRelevant 过滤 → 进 RAF buffer → 持续渲染循环')
      console.log('       → 后台 RAF 停 → 积压 → 切回爆发（解释"后台切回也卡"）')
    } else {
      console.log('active pipeline 未在推流（可能推流的是别的 pipeline）')
    }
  }

  writeFileSync('tests/probe_d_output.json', JSON.stringify({
    persisted, probe, pushPids,
  }, null, 2))
  console.log('\n写入 tests/probe_d_output.json')
  await browser.close()
}
main().catch((e) => { console.error(e); process.exit(1) })
