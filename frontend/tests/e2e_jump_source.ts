import { chromium } from 'playwright'

async function main() {
  const browser = await chromium.launch({ headless: false, args: ['--start-maximized'] })
  const context = await browser.newContext({ viewport: null })
  const page = await context.newPage()

  const initScript = [
    '() => {',
    '  window.__scrollSets = [];',
    '  window.__samples = [];',
    '  var proto = HTMLElement.prototype;',
    '  var desc = Object.getOwnPropertyDescriptor(proto, "scrollTop");',
    '  if (desc && desc.set) {',
    '    var origSet = desc.set;',
    '    Object.defineProperty(proto, "scrollTop", {',
    '      configurable: true,',
    '      get: desc.get,',
    '      set: function(v) {',
    '        window.__scrollSets.push({',
    '          t: Date.now(),',
    '          value: v,',
    '          stack: (new Error().stack||"").split("\\n").slice(1,10)',
    '            .map(function(s){return s.trim().replace(/^at |@/g,"").slice(0,70)}).join(" |> ")',
    '        });',
    '        return origSet.call(this, v);',
    '      }',
    '    });',
    '  }',
    '}',
  ].join('\n')
  await context.addInitScript(initScript)

  await page.goto('http://localhost:5289', { waitUntil: 'networkidle', timeout: 30000 })
  await page.fill('[data-testid="login-username-input"]', 'admin')
  await page.fill('[data-testid="login-password-input"]', 'admin123')
  await page.click('form button')
  await page.waitForTimeout(5000)

  const enterScript = [
    '() => {',
    '  var spans = Array.from(document.querySelectorAll("span, div, button"));',
    '  var t = spans.find(function(s){return (s.textContent||"").includes("真实修仙游戏") && s.children.length===0});',
    '  if (t) t.click();',
    '}',
  ].join('\n')
  await page.evaluate(enterScript)
  await page.waitForTimeout(4000)

  await page.evaluate('() => { window.__scrollSets = []; window.__samples = [] }')
  console.log('=== 刷新 ===')
  await page.reload({ waitUntil: 'domcontentloaded', timeout: 60000 })

  const sampleScript = [
    '() => {',
    '  var ml = document.querySelector("[data-testid=message-list]");',
    '  var ch = ml ? ml.clientHeight : 0;',
    '  var sh = ml ? ml.scrollHeight : 0;',
    '  var st = ml ? ml.scrollTop : 0;',
    '  return {st:st, sh:sh, ch:ch, atBottom:(sh-st-ch)<50, max:sh-ch};',
    '}',
  ].join('\n')

  const t0 = Date.now()
  const samples: any[] = []
  for (let i = 0; i < 60; i++) {
    const s = await page.evaluate(sampleScript).catch(() => null)
    if (s) { (s as any).t = Date.now() - t0; samples.push(s) }
    await page.waitForTimeout(50)
  }

  const data = { sets: await page.evaluate(() => (window as any).__scrollSets || []).catch(() => []), samples } as any

  console.log('\n=== scrollTop 采样（t=ms）===')
  let prev: any = null
  for (const s of data.samples) {
    if (!prev || Math.abs(s.st - prev.st) > 2 || Math.abs(s.sh - prev.sh) > 2) {
      const mark = s.atBottom ? '底' : (s.st > 0 ? '中' : '?')
      console.log('  t=' + String(s.t).padStart(5) + ' st=' + String(s.st).padStart(6) + ' sh=' + String(s.sh).padStart(6) + ' max=' + String(s.max).padStart(6) + ' [' + mark + ']')
    }
    prev = s
  }

  console.log('\n=== scrollTop 赋值次数: ' + data.sets.length + ' ===')
  const jumps = data.sets.filter((s: any) => s.value > 500)
  const show = jumps.length > 0 ? jumps : data.sets
  for (const j of show.slice(0, 6)) {
    console.log('  t=' + (j.t - t0) + ' value=' + j.value)
    console.log('    栈: ' + (j.stack || '').slice(0, 200))
  }

  await browser.close()
}

main().catch(e => { console.error(e); process.exit(1) })
