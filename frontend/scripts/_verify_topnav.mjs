import { chromium } from 'playwright-core'
import path from 'path'
import { fileURLToPath } from 'url'
import { mkdirSync } from 'fs'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const outDir = path.resolve(__dirname, '../../.workbuddy/screenshots')
mkdirSync(outDir, { recursive: true })

const browser = await chromium
  .launch({ headless: true, channel: 'msedge' })
  .catch(() => chromium.launch({ headless: true }))
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
page.on('pageerror', (e) => console.log('PAGEERROR', e.message))
page.on('console', (m) => {
  if (m.type() === 'error') console.log('CERR', m.text().slice(0, 160))
})

await page.goto('http://localhost:5290/', { waitUntil: 'domcontentloaded' })
await page.waitForSelector('[data-testid=app-header]', { timeout: 20000 })
await page.evaluate(() => {
  localStorage.setItem(
    'layout-mode-storage',
    JSON.stringify({ state: { mode: 'five-space' }, version: 0 }),
  )
  localStorage.setItem(
    'theme-storage',
    JSON.stringify({ state: { currentThemeId: 'dark', mode: 'dark' }, version: 0 }),
  )
  // clear workspace tabs so defaults re-inject cleanly
  try {
    const raw = localStorage.getItem('layout-mode-storage')
    const o = raw ? JSON.parse(raw) : { state: {}, version: 0 }
    o.state = { ...(o.state || {}), mode: 'five-space', workspaceTabs: [] }
    localStorage.setItem('layout-mode-storage', JSON.stringify(o))
  } catch {
    /* ignore */
  }
})
await page.reload({ waitUntil: 'domcontentloaded' })
await page.waitForSelector('[data-testid=app-header]', { timeout: 20000 })
await page.waitForTimeout(2500)

const toggleCount = await page.locator('[data-testid=titlebar-toggle-sidebar]').count()
console.log('toggle count', toggleCount)

// click top-nav 设置
await page.getByRole('button', { name: '设置', exact: true }).first().click()
await page.waitForTimeout(1800)

const tabLabels = await page.evaluate(() =>
  Array.from(document.querySelectorAll('.border-b-2'))
    .map((el) => (el.textContent || '').replace(/\s+/g, ' ').trim())
    .filter(Boolean),
)
console.log('tabs', tabLabels)
const hub = await page.locator('[data-testid=settings-hub]').count()
console.log('settings-hub', hub)
await page.screenshot({ path: path.join(outDir, 'verify-topnav-settings.png') })

// open monitoring
await page.getByRole('button', { name: '监控', exact: true }).first().click()
await page.waitForTimeout(1200)
const tabLabels2 = await page.evaluate(() =>
  Array.from(document.querySelectorAll('.border-b-2'))
    .map((el) => (el.textContent || '').replace(/\s+/g, ' ').trim())
    .filter(Boolean),
)
console.log('tabs after monitoring', tabLabels2)
await page.screenshot({ path: path.join(outDir, 'verify-topnav-monitoring.png') })

// collapse sidebar
await page.locator('[data-testid=titlebar-toggle-sidebar]').click()
await page.waitForTimeout(600)
const widths = await page.evaluate(() =>
  Array.from(document.querySelectorAll('aside')).map((a) => Math.round(a.getBoundingClientRect().width)),
)
console.log('aside widths', widths)
await page.screenshot({ path: path.join(outDir, 'verify-sidebar-collapsed.png') })

await browser.close()
console.log('ok')
