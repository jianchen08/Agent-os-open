// @feature: mobile-v1.10 主题持久化 | @ci: frontend-test
/** 壳主题桥访问器行为：notifyShellTheme 上报与 getShellLastTheme 查询的降级面 */
import { describe, expect, it, vi } from 'vitest'

type PluginsBag = Record<string, unknown> | undefined

function withCapacitor(plugins: PluginsBag) {
  ;(window as unknown as { Capacitor?: unknown }).Capacitor = plugins
    ? { Plugins: plugins }
    : undefined
}

async function importSut() {
  return import('../shellPlugins')
}

describe('notifyShellTheme', () => {
  it('无 Capacitor（浏览器/桌面）→ 空转不抛错', async () => {
    withCapacitor(undefined)
    const { notifyShellTheme } = await importSut()
    await expect(notifyShellTheme('dark')).resolves.toBeUndefined()
  })

  it('有壳桥 → 以 { themeId } 透传调用', async () => {
    const notify = vi.fn().mockResolvedValue(undefined)
    withCapacitor({ ShellIntents: { notifyShellTheme: notify } })
    const { notifyShellTheme } = await importSut()
    await notifyShellTheme('deep-space')
    expect(notify).toHaveBeenCalledWith({ themeId: 'deep-space' })
  })

  it('桥 rejects → 吞掉不向上抛（切换不被阻塞）', async () => {
    const notify = vi.fn().mockRejectedValue(new Error('bridge down'))
    withCapacitor({ ShellIntents: { notifyShellTheme: notify } })
    const { notifyShellTheme } = await importSut()
    await expect(notifyShellTheme('dark')).resolves.toBeUndefined()
  })

  it('桩上无 notifyShellTheme 方法（旧壳包）→ 空转', async () => {
    withCapacitor({ ShellIntents: {} })
    const { notifyShellTheme } = await importSut()
    await expect(notifyShellTheme('dark')).resolves.toBeUndefined()
  })
})

describe('getShellLastTheme', () => {
  it('无 Capacitor（浏览器/桌面）→ null（恢复链路不激活）', async () => {
    withCapacitor(undefined)
    const { getShellLastTheme } = await importSut()
    await expect(getShellLastTheme()).resolves.toBeNull()
  })

  it('壳有存值 → 返回该 id', async () => {
    withCapacitor({
      ShellIntents: { getLastTheme: vi.fn().mockResolvedValue({ themeId: 'deep-space' }) },
    })
    const { getShellLastTheme } = await importSut()
    await expect(getShellLastTheme()).resolves.toBe('deep-space')
  })

  it('壳存值为空串（真·首装）→ null', async () => {
    withCapacitor({ ShellIntents: { getLastTheme: vi.fn().mockResolvedValue({ themeId: '' }) } })
    const { getShellLastTheme } = await importSut()
    await expect(getShellLastTheme()).resolves.toBeNull()
  })

  it('桥 rejects → null（调用方跳过恢复）', async () => {
    withCapacitor({
      ShellIntents: { getLastTheme: vi.fn().mockRejectedValue(new Error('bridge down')) },
    })
    const { getShellLastTheme } = await importSut()
    await expect(getShellLastTheme()).resolves.toBeNull()
  })
})
