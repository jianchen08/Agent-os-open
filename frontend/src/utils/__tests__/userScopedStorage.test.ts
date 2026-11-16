/**
 * @feature: FP-0.2.用户隔离 | @ci: frontend-test
 * 账户命名空间单点（隔离执法点①）单元测试
 *
 * 契约：docs/working/用户隔离机制_审计与修复_20261018.md §5 前端执法点 1。
 * - 无身份 → ag-anon: 前缀（登录页瞬态，不落任何账户数据）
 * - 有身份 → ag-u:<userId>:<base>（不同账户读写互不可达）
 * - 注册表缺席时回退读 localStorage AUTH_USER（页面刷新恢复路径）
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'

const { userScopedKey, currentCacheUserId, setCacheUserId } = await import(
  '@/utils/userScopedStorage'
)

describe('userScopedStorage 账户命名空间', () => {
  beforeEach(() => {
    localStorage.clear()
    setCacheUserId(null)
  })

  it('无身份时键落 ag-anon 命名空间（登录页瞬态态）', () => {
    expect(userScopedKey('pipeline-messages')).toBe('ag-anon:pipeline-messages')
    expect(currentCacheUserId()).toBeNull()
  })

  it('有身份时键注入账户前缀，不同账户键互不相同', () => {
    setCacheUserId('u-aaaa')
    const keyA = userScopedKey('pipeline-messages')
    setCacheUserId('u-bbbb')
    const keyB = userScopedKey('pipeline-messages')
    expect(keyA).toBe('ag-u:u-aaaa:pipeline-messages')
    expect(keyB).toBe('ag-u:u-bbbb:pipeline-messages')
    expect(keyA).not.toBe(keyB)
  })

  it('注册表缺席时回退读 localStorage AUTH_USER（刷新恢复路径）', async () => {
    // 「注册表尚未写入」是模块初载态：取全新模块实例验证（模块态不跨用例伪造）
    vi.resetModules()
    const fresh = await import('@/utils/userScopedStorage')
    localStorage.setItem('auth_user', JSON.stringify({ id: 'u-from-storage' }))
    expect(fresh.currentCacheUserId()).toBe('u-from-storage')
    expect(fresh.userScopedKey('layout-mode')).toBe('ag-u:u-from-storage:layout-mode')
    // 恢复共享模块注册表（后续用例依赖）
    setCacheUserId(null)
  })

  it('注册表身份优先于存储回退（登录后身份与磁盘可能短暂不一致）', () => {
    localStorage.setItem('auth_user', JSON.stringify({ id: 'u-stale' }))
    setCacheUserId('u-current')
    expect(currentCacheUserId()).toBe('u-current')
  })

  it('畸形 AUTH_USER（非 JSON/无 id）按无身份处理不抛异常', () => {
    localStorage.setItem('auth_user', 'not-json{')
    expect(currentCacheUserId()).toBeNull()
    localStorage.setItem('auth_user', JSON.stringify({ username: 'x' }))
    expect(currentCacheUserId()).toBeNull()
  })

  it('显式登出（null）优先于存储残影', () => {
    localStorage.setItem('auth_user', JSON.stringify({ id: 'u-ghost' }))
    setCacheUserId(null)
    expect(currentCacheUserId()).toBeNull()
  })

  // 防拟合：字面值断言配一条性质断言——键函数对任意 base 只注入前缀不改 base
  it('性质：任意 base 键恒以同一命名空间前缀开头且以 base 结尾', () => {
    setCacheUserId('u-x')
    for (const base of ['a', 'chat-input-drafts', 'very/odd:key with spaces']) {
      const k = userScopedKey(base)
      expect(k.startsWith('ag-u:u-x:')).toBe(true)
      expect(k.endsWith(`:${base}`)).toBe(true)
    }
  })

  it('性质：静默调用无副作用告警（纯函数面）', () => {
    const spy = vi.spyOn(console, 'warn')
    userScopedKey('x')
    expect(spy).not.toHaveBeenCalled()
    spy.mockRestore()
  })
})
