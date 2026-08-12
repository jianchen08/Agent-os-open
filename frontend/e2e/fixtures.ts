/**
 * 共享 e2e test 夹具。
 *
 * T5#14 修复：每个测试结束后自动清理浏览器存储与 cookie，避免测试间状态泄漏
 * （此前 10/14 spec 无 afterEach/beforeEach 清理钩子）。各 spec 把
 *   import { test, expect } from '@playwright/test'
 * 改为
 *   import { test, expect } from '../fixtures'
 * 即获得自动清理，无需各自手写 afterEach。
 *
 * 注：Playwright 默认每测试用独立 browser context（localStorage/cookie 已隔离），
 * 此 afterEach 进一步显式清空 storage + cookie + 登出态，覆盖"同 spec 内多 test 复用
 * 页面/共享 storage key"的场景，并作为语义自文档（每个 test 都以干净状态起止）。
 */
import { test as base, expect } from '@playwright/test'

export const test = base

// 每个测试后清理浏览器侧可变状态（localStorage / sessionStorage / cookie）。
test.afterEach(async ({ page, context }) => {
  try {
    await page.evaluate(() => {
      localStorage.clear()
      sessionStorage.clear()
    })
  } catch {
    // 页面可能已关闭、或尚未导航到 http origin（about:blank 不支持 localStorage），忽略。
  }
  await context.clearCookies().catch(() => {
    // context 可能已关闭，忽略。
  })
})

export { expect }
