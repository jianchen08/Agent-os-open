/**
 * 判据 F：用 CDP Profiler 抓主线程 long task 的真实调用栈
 *
 * 判据 D 发现 1 个 238ms long task（干净 profile、登录后稳定期）。
 * 判据 C 已证明 onmessage/store/handler 都 <1.2ms，所以 long task 在渲染/effect 路径。
 * 用 CDP Performance.profile 抓这段的 CPU profile，导出后分析热点函数。
 *
 * 纯只读，被动观察。
 */
import { chromium } from 'playwright'
import { writeFileSync } from 'fs'

const API = 'http://localhost:5289'

async function main() {
  const browser = await chromium.launch({ headless: true })
  const context = await browser.newContext({ viewport: { width: 1600, height: 900 } })
  const page = await context.newPage()

  const client = await context.newCDPSession(page)
  await client.send('Profiler.enable')
  await client.send('Performance.enable')

  // long task 观察器
  await client.send('Performance.enable')
  const longs = []
  // 用 Performance.metrics + 手动采样不直接给栈。
  // 改用：Profiler 采样，事后分析 selfTime 最大的函数。

  await page.goto(API, { waitUntil: 'networkidle', timeout: 30000 })
  const li = await page.locator('[data-testid="login-username-input"]').count()

  // 登录前就开始 profile，覆盖登录后那段（long task 多发于此）
  await client.send('Profiler.start')
  console.log('[F] Profiler 已启动')

  if (li > 0) {
    await page.fill('[data-testid="login-username-input"]', 'admin')
    await page.fill('[data-testid="login-password-input"]', 'admin123')
    await page.click('form button')
  }
  console.log('[F] 登录后观察 20s ...')
  await page.waitForTimeout(20000)

  const { profile } = await client.send('Profiler.stop')
  console.log('\n===== 判据 F：CPU Profile 热点 =====')

  // 分析：累计 selfTime 最大的函数节点
  const nodes = profile.nodes
  const samples = profile.samples || []
  const timeDeltas = profile.timeDeltas || []
  const nodeIdByCallFrame = {}

  // 计算每个节点的采样命中次数（≈ self time）
  const hitCount = {}
  for (const id of samples) {
    hitCount[id] = (hitCount[id] || 0) + 1
  }

  const nodeById = {}
  for (const n of nodes) nodeById[n.id] = n

  // 排序：按命中次数
  const ranked = Object.entries(hitCount)
    .map(([id, hits]) => {
      const n = nodeById[id]
      const cf = n.callFrame
      return {
        hits,
        fn: cf.functionName || '(anonymous)',
        url: cf.url || '',
        line: cf.lineNumber,
      }
    })
    .sort((a, b) => b.hits - a.hits)

  const totalSamples = samples.length
  const intervalUs = (profile.samples && timeDeltas.length)
    ? Math.round(timeDeltas.reduce((a, b) => a + b, 0) / timeDeltas.length)
    : 100

  console.log(`总采样: ${totalSamples}，采样间隔≈${intervalUs}μs，profile 总时长≈${(totalSamples * intervalUs / 1000).toFixed(0)}ms`)
  console.log(`\nselfTime 最大的 25 个函数（占总 CPU 的占比）:`)
  console.log('  hits  pct    function                          file:line')
  for (const r of ranked.slice(0, 25)) {
    const pct = (r.hits / totalSamples * 100).toFixed(1)
    const fn = (r.fn || '(anon)').padEnd(34).slice(0, 34)
    const file = r.url.split('/').slice(-2).join('/')
    console.log(`  ${String(r.hits).padStart(5)} ${pct.padStart(5)}%  ${fn} ${file}:${r.line}`)
  }

  // 按"文件前缀"聚合（模块级热点）
  const byModule = {}
  for (const r of ranked) {
    if (!r.url) { byModule['(unknown)'] = (byModule['(unknown)'] || 0) + r.hits; continue }
    const m = r.url.split('/').slice(-3).join('/').split('?')[0]
    byModule[m] = (byModule[m] || 0) + r.hits
  }
  console.log(`\n按模块聚合（selfTime 占比）:`)
  Object.entries(byModule).sort((a, b) => b[1] - a[1]).slice(0, 15).forEach(([m, h]) => {
    console.log(`  ${(h / totalSamples * 100).toFixed(1).padStart(5)}%  ${h.toString().padStart(5)}  ${m}`)
  })

  writeFileSync('tests/probe_f_profile.cpuprofile', JSON.stringify(profile))
  console.log('\n完整 profile 写入 tests/probe_f_profile.cpuprofile（可拖入 Chrome DevTools Performance 查看）')
  await browser.close()
}
main().catch((e) => { console.error(e); process.exit(1) })
