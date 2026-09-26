/**
 * @feature FP-0.2.四 前端Schema | @vision V6 可即用 | @audit T5#2 | @ci frontend-e2e
 *
 * CI 冒烟（零外部依赖）：验证前端构建产物可服务、React 正常挂载、页面渲染实质内容。
 * 不依赖 kernel/LLM 后端——仅验证"构建 + 服务 + 渲染"这一可冒烟的契约，作为 PR 级
 * e2e 门禁。journey_* 全流程需后端，仍归本地/手动（见 e2e-manual）。
 * 回应 T5#2（playwright 无 job）/ T5#5（e2e-manual 语义造假）。
 */
import { test, expect } from '../fixtures'

test.describe('CI 冒烟（零外部依赖）', () => {
  test('前端构建产物可服务且 React 挂载', async ({ page }) => {
    const errors: string[] = []
    page.on('pageerror', (err) => errors.push(err.message))

    await page.goto('/', { waitUntil: 'domcontentloaded' })

    // #root 被 React 挂载后非空（非白屏崩溃）
    await expect(page.locator('#root')).not.toBeEmpty({ timeout: 30_000 })

    // 渲染出实质内容（非空白页）。web-first 断言（执行方案批次7-①）：
    // toContainText 自动重试，不再依赖「not.toBeEmpty 后一次性读 innerText」
    // 的非同步组合——首屏「路由内容整树卸载」的 ~4ms 窗口曾致 8.3% flake
    //（评估 §0.3(7)），#root 卸载窗口期本断言会重试而非失败。
    await expect(page.locator('#root')).toContainText(/\S/, { timeout: 30_000 })

    // 无致命未捕获 JS 错误（过滤后端缺失导致的网络错误）
    const fatal = errors.filter((e) => !e.includes('net::') && !e.includes('Network Error'))
    expect(fatal, `页面有未捕获错误: ${fatal.join('; ')}`).toHaveLength(0)
  })
})
