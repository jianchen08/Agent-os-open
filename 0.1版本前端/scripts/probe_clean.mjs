// 干净测量版：不开 CDP Tracing（消除 observer effect），只用轻量页面探针 + CDP WS 计数。
// 目的：拿到不受干扰的真实帧率/长任务，确认流式期间到底卡不卡。
import { chromium } from 'playwright'
import { writeFileSync, mkdirSync } from 'fs'
import { join } from 'path'

const FRONTEND = process.env.PROBE_FRONTEND || 'http://localhost:5290'
const OUT_DIR = process.env.PROBE_OUT || join(process.cwd(), 'probe-out')
const HEADLESS = process.env.PROBE_HEADLESS !== '0'
const PROMPT = process.env.PROBE_PROMPT ||
  '请写一段较长的内容：用 markdown 输出 3 段文字、一个包含 20 行代码的 python 代码块、一个二级标题、和一个 3x3 的表格。要求内容充实，总字数 600 字以上。'
mkdirSync(OUT_DIR, { recursive: true })

const PROBE_CODE = `
window.__p = { start: performance.now(), frames: 0, janky: 0, long: [], longBuckets: {} };
(function(){
  if (window.__pa) return; window.__pa = true;
  const S = window.__p;
  try {
    new PerformanceObserver((l)=>{
      for (const e of l.getEntries()) if (e.duration > 50) {
        S.long.push({t: Math.round(e.duration), at: Math.round(e.startTime)});
        const b = e.duration >= 250 ? '250ms+' : (e.duration >= 100 ? '100-250ms' : '50-100ms');
        S.longBuckets[b] = (S.longBuckets[b]||0)+1;
      }
    }).observe({ entryTypes: ['longtask'] });
  } catch(_) {}
  let last = performance.now();
  (function loop(){
    if (!window.__pa) return;
    S.frames++;
    const now = performance.now(), gap = now - last;
    if (gap > 20) S.janky++;
    last = now;
    requestAnimationFrame(loop);
  })();
})();
`

async function main() {
  const browser = await chromium.launch({ headless: HEADLESS })
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } })
  await context.addInitScript(PROBE_CODE)
  const page = await context.newPage()

  // CDP 只开 Network（轻量），不开 Tracing
  const client = await context.newCDPSession(page)
  await client.send('Network.enable')
  const wsByType = {}, wsCount = { v: 0 }
  client.on('Network.webSocketFrameReceived', ({ response }) => {
    wsCount.v++
    try { const t = JSON.parse(response.payloadData)?.type; if (t) wsByType[t] = (wsByType[t]||0)+1 } catch(_) {}
  })

  await page.goto(FRONTEND, { waitUntil: 'domcontentloaded', timeout: 60000 })
  await page.locator('[data-testid="login-username-input"]').waitFor({ state: 'visible', timeout: 30000 })
  await page.locator('[data-testid="login-username-input"]').pressSequentially('admin', { delay: 20 })
  await page.locator('[data-testid="login-password-input"]').pressSequentially('admin123', { delay: 20 })
  await page.locator('button[type="submit"]').first().click()
  await page.waitForURL((u) => !u.pathname.includes('/login'), { timeout: 30000 })
  await page.waitForTimeout(3000)
  await page.evaluate(() => {
    const s = document.querySelector('[aria-label^="会话:"]')
    if (s) ['pointerdown','mousedown','pointerup','mouseup','click'].forEach((t) =>
      s.dispatchEvent(new MouseEvent(t, { bubbles: true, cancelable: true, view: window })))
  })
  await page.waitForTimeout(3000)

  const input = page.locator('[data-testid="chat-input-textarea"]').first()
  await input.waitFor({ state: 'visible', timeout: 30000 })
  await input.click(); await input.fill(PROMPT); await page.waitForTimeout(500)
  console.log('[probe] 发送消息')
  await input.press('Enter')

  // 采样
  const samples = []
  const t0 = Date.now()
  let lastWs = 0, stable = 0
  while (Date.now() - t0 < 50000) {
    await page.waitForTimeout(2000)
    const s = await page.evaluate(() => {
      const S = window.__p, dur = (performance.now() - S.start) / 1000
      return { dur: dur.toFixed(1), fps: (S.frames/dur).toFixed(0), janky: S.janky,
        long: S.long.length, buckets: {...S.longBuckets} }
    })
    const wsNow = wsCount.v
    const top = Object.entries(wsByType).sort((a,b)=>b[1]-a[1]).slice(0,5).map(([k,v])=>`${k}=${v}`).join(' ')
    samples.push({ ...s, ws: wsNow, wsDelta: wsNow - lastWs })
    console.log(`[${s.dur}s] WS=${wsNow}(+${wsNow-lastWs}) fps≈${s.fps} janky(>20ms)=${s.janky} longTask(>50ms)=${s.long} ${JSON.stringify(s.buckets)} | ${top}`)
    if (wsNow === lastWs) { stable++; if (stable >= 2 && wsByType.stream_end) { console.log('[probe] 流式结束'); break } }
    else stable = 0
    lastWs = wsNow
  }

  const final = await page.evaluate(() => {
    const S = window.__p, dur = (performance.now() - S.start) / 1000
    return { dur, fps: S.frames/dur, janky: S.janky, long: S.long,
      buckets: S.longBuckets, topLong: S.long.slice().sort((a,b)=>b.t-a.t).slice(0,10) }
  })
  writeFileSync(join(OUT_DIR, 'clean-report.json'), JSON.stringify({ samples, final, wsByType, wsTotal: wsCount.v }, null, 2))

  console.log('\n========== 干净测量汇总（无 Tracing 干扰）==========')
  console.log(`时长: ${final.dur.toFixed(1)}s | WS 总帧: ${wsCount.v}`)
  console.log(`真实 fps: ${final.fps.toFixed(1)} | 丢帧(>20ms): ${final.janky}`)
  console.log(`长任务(>50ms): ${final.long.length} 次 ${JSON.stringify(final.buckets)}`)
  console.log(`流式 chunk 数: stream_chunk=${wsByType.stream_chunk||0} thinking_chunk=${wsByType.thinking_chunk||0}`)
  console.log('Top 长任务:', final.topLong.slice(0,5))
  await browser.close()
}
main().catch((e) => { console.error(e); process.exit(1) })
