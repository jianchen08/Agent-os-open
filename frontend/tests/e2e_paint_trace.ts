import { chromium } from 'playwright'

async function main() {
  const b = await chromium.launch({ headless: false })
  const p = await (await b.newContext({ viewport: null })).newPage()
  await p.goto('http://localhost:5289', { waitUntil: 'networkidle', timeout: 30000 })
  await p.fill('[data-testid="login-username-input"]', 'admin')
  await p.fill('[data-testid="login-password-input"]', 'admin123')
  await p.click('form button')
  await p.waitForTimeout(5000)
  await p.evaluate(() => {
    const s = Array.from(document.querySelectorAll('span,div,button'))
    const t = s.find((x: any) => (x.textContent || '').includes('真实修仙游戏') && x.children.length === 0)
    if (t) (t as HTMLElement).click()
  })
  await p.waitForTimeout(3000)

  // 先确认 build tag
  const tag = await p.evaluate(() => (window as any).__ML_BUILD_TAG || 'NOT_FOUND')
  console.log('浏览器加载的 build tag:', tag)

  // 刷新后用 PerformanceObserver 抓首次 paint，并在每个 paint 后立刻读 scrollTop
  console.log('\n=== 刷新，抓每个 paint 后的 scrollTop ===')
  await p.reload({ waitUntil: 'domcontentloaded', timeout: 60000 })

  // 用 requestAnimationFrame 循环在浏览器侧记录（最贴近真实渲染帧）
  const trace = await p.evaluate(() => {
    return new Promise<any>((resolve) => {
      const samples: any[] = []
      const t0 = Date.now()
      const tick = () => {
        const ml = document.querySelector('[data-testid=message-list]') as HTMLElement | null
        if (ml) {
          samples.push({
            t: Date.now() - t0,
            st: ml.scrollTop,
            sh: ml.scrollHeight,
            ch: ml.clientHeight,
            atBottom: ml.scrollHeight - ml.scrollTop - ml.clientHeight < 80,
          })
        }
        if (Date.now() - t0 < 2000) {
          requestAnimationFrame(tick)
        } else {
          resolve(samples)
        }
      }
      requestAnimationFrame(tick)
    })
  })

  console.log('t(ms) | scrollTop | maxScroll | atBottom | Δ(距底)')
  let prev: any = null
  for (const s of trace) {
    const maxScroll = s.sh - s.ch
    const distToBottom = maxScroll - s.st
    if (!prev || Math.abs(s.st - prev.st) > 5 || s.atBottom !== prev.atBottom) {
      const mark = s.atBottom ? '✅底' : '⚠️' + (distToBottom > 0 ? '距底' + distToBottom : '')
      console.log(`  ${String(s.t).padStart(5)} | ${String(s.st).padStart(8)} | ${String(maxScroll).padStart(8)} | ${String(s.atBottom).padEnd(5)} | ${mark}`)
    }
    prev = s
  }
  await b.close()
}
main().catch((e) => { console.error(e); process.exit(1) })
