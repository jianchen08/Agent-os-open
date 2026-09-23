// @feature: FP-0.2.五 审批闭环 | @ci: frontend-test
/**
 * showSystemNotification 系统通知工具测试
 *
 * 覆盖：非 Electron 环境（无 electronAPI）、旧版壳无 notification 桥接、
 * 正常透传 title/body、invoke 拒绝不上抛、免打扰开关拦截、localStorage
 * 不可用按未静音继续。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { showSystemNotification } from '@/utils/systemNotification'

type NotificationBridge = {
  show: (opts: { title: string; body: string }) => Promise<boolean>
}

function setBridge(bridge: NotificationBridge | undefined): void {
  if (bridge === undefined) {
    delete (window as { electronAPI?: unknown }).electronAPI
    return
  }
  ;(window as { electronAPI?: unknown }).electronAPI = { notification: bridge }
}

describe('showSystemNotification', () => {
  beforeEach(() => {
    localStorage.removeItem('notification_sound_muted')
    vi.restoreAllMocks()
  })

  it('非 Electron（无 electronAPI）→ false', async () => {
    setBridge(undefined)
    await expect(
      showSystemNotification({ title: '审批', body: '请确认' }),
    ).resolves.toBe(false)
  })

  it('electronAPI 无 notification 桥接（旧版壳）→ false', async () => {
    ;(window as { electronAPI?: unknown }).electronAPI = {}
    await expect(
      showSystemNotification({ title: '审批', body: '请确认' }),
    ).resolves.toBe(false)
  })

  it('桥接可用 → true 且原样透传 title/body', async () => {
    const show = vi.fn(async (opts: { title: string; body: string }) => opts.title.length > 0)
    setBridge({ show })
    await expect(
      showSystemNotification({ title: '审批：删除目录', body: 'agent-x 请求您的输入' }),
    ).resolves.toBe(true)
    expect(show).toHaveBeenCalledTimes(1)
    expect(show).toHaveBeenCalledWith({
      title: '审批：删除目录',
      body: 'agent-x 请求您的输入',
    })
  })

  it('桥接返回 false（宿主不支持/参数非法）→ 原样透传 false', async () => {
    setBridge({ show: vi.fn(async () => false) })
    await expect(
      showSystemNotification({ title: '审批', body: '请确认' }),
    ).resolves.toBe(false)
  })

  it('invoke 拒绝 → 折算为 false，不上抛', async () => {
    setBridge({ show: vi.fn(async () => { throw new Error('ipc down') }) })
    await expect(
      showSystemNotification({ title: '审批', body: '请确认' }),
    ).resolves.toBe(false)
  })

  it('免打扰（notification_sound_muted=true）→ false 且不触达桥接', async () => {
    localStorage.setItem('notification_sound_muted', 'true')
    const show = vi.fn(async () => true)
    setBridge({ show })
    await expect(
      showSystemNotification({ title: '审批', body: '请确认' }),
    ).resolves.toBe(false)
    expect(show).not.toHaveBeenCalled()
  })

  it('localStorage.getItem 抛异常 → 按未静音继续走桥接', async () => {
    const show = vi.fn(async () => true)
    setBridge({ show })
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('storage blocked')
    })
    await expect(
      showSystemNotification({ title: '审批', body: '请确认' }),
    ).resolves.toBe(true)
    expect(show).toHaveBeenCalledTimes(1)
  })
})
