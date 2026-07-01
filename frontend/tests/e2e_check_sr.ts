import { chromium } from 'playwright'
async function main() {
  const b = await chromium.launch({ headless: false })
  const p = await (await b.newContext({ viewport: null })).newPage()
  await p.goto('http://localhost:5289', { waitUntil: 'networkidle', timeout: 30000 })
  const sr = await p.evaluate(() => history.scrollRestoration)
  console.log('scrollRestoration =', sr)
  await b.close()
}
main().catch((e) => { console.error(e); process.exit(1) })
