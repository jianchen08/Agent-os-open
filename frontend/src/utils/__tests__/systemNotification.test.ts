// @feature: FP-0.2.五 审批闭环 | @ci: frontend-test
/**
 * showSystemNotification 系统通知工具测试
 *
 * 覆盖：非 Electron 环境（无 electronAPI）、旧版壳无 notification 桥接、
 * 正常透传 title/body、invoke 拒绝不上抛、免打扰开关拦截、localStorage
 * 不可用按未静音继续；threadId 透传；通知点击导航（handleNotificationNavigate
 * 打开链与非法载荷退化）与装配订阅（installNotificationNavigateListener）。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import {
  handleNotificationNavigate,
  installNotificationNavigateListener,
  showSystemNotification,
} from '@/utils/systemNotification'

const sessionListActions = vi.hoisted(() => ({
  rebuildSessionFromThread: vi.fn(),
  setActiveSession: vi.fn(async () => undefined),
}))
vi.mock('@/stores/sessionListStore', () => ({
  useSessionListStore: { getState: () => sessionListActions },
}))

type NotificationBridge = {
  show: (opts: { title: string; body: string; threadId?: string }) => Promise<boolean>
  onNotificationNavigate?: (
    callback: (payload: { threadId?: string }) => void,
  ) => () => void
}

function setBridge(bridge: NotificationBridge | undefined): void {
  if (bridge === undefined) {
    delete (window as { electronAPI?: unknown }).electronAPI
    return
  }
  ;(window as { electronAPI?: unknown }).electronAPI = { notification: bridge }
}

function clearBridge(): void {
  delete (window as { electronAPI?: unknown }).electronAPI
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

  it('携带 threadId → 原样透传给桥接（点击跳转载荷）', async () => {
    const show = vi.fn(async () => true)
    setBridge({ show })
    await expect(
      showSystemNotification({ title: '审批', body: '请确认', threadId: 'th-1' }),
    ).resolves.toBe(true)
    expect(show).toHaveBeenCalledWith({
      title: '审批',
      body: '请确认',
      threadId: 'th-1',
    })
  })
})

describe('handleNotificationNavigate（通知点击 → 打开对应对话）', () => {
  beforeEach(() => {
    clearBridge()
    vi.restoreAllMocks()
    sessionListActions.rebuildSessionFromThread.mockReset()
    sessionListActions.setActiveSession.mockReset().mockResolvedValue(undefined)
  })

  it('合法 threadId → 重建会话条目并切换打开（10e799226 打开链）', () => {
    handleNotificationNavigate({ threadId: 'th-9' })
    expect(sessionListActions.rebuildSessionFromThread).toHaveBeenCalledTimes(1)
    expect(sessionListActions.rebuildSessionFromThread).toHaveBeenCalledWith('th-9')
    expect(sessionListActions.setActiveSession).toHaveBeenCalledTimes(1)
    expect(sessionListActions.setActiveSession).toHaveBeenCalledWith('th-9')
  })

  it.each([
    ['载荷缺失', undefined],
    ['threadId 缺失', {}],
    ['threadId 空串', { threadId: '' }],
    ['threadId 非字符串', { threadId: 42 }],
  ])('%s → 不触碰会话 store（退化仅聚焦）', (_name, payload) => {
    handleNotificationNavigate(payload as { threadId?: unknown } | undefined)
    expect(sessionListActions.rebuildSessionFromThread).not.toHaveBeenCalled()
    expect(sessionListActions.setActiveSession).not.toHaveBeenCalled()
  })

  it('打开失败 → 留痕不上抛（窗口已被壳层聚焦，零 unhandled rejection）', async () => {
    const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {})
    sessionListActions.setActiveSession.mockRejectedValue(new Error('switch boom'))
    handleNotificationNavigate({ threadId: 'th-boom' })
    await vi.waitFor(() =>
      expect(errSpy).toHaveBeenCalledWith(
        '[SystemNotification] 通知点击打开会话失败:',
        'th-boom',
        expect.any(Error),
      ),
    )
    errSpy.mockRestore()
  })
})

describe('installNotificationNavigateListener（应用装配订阅）', () => {
  beforeEach(() => {
    clearBridge()
    vi.restoreAllMocks()
    sessionListActions.rebuildSessionFromThread.mockReset()
    sessionListActions.setActiveSession.mockReset().mockResolvedValue(undefined)
  })

  it('桥接可用 → 订阅后导航载荷路由到打开链，退订函数生效', () => {
    let handler: ((payload: { threadId?: string }) => void) | null = null
    const disposer = vi.fn()
    const onNotificationNavigate = vi.fn((cb: (p: { threadId?: string }) => void) => {
      handler = cb
      return disposer
    })
    setBridge({ show: vi.fn(async () => true), onNotificationNavigate })

    const unsubscribe = installNotificationNavigateListener()
    expect(onNotificationNavigate).toHaveBeenCalledTimes(1)

    handler!({ threadId: 'th-nav' })
    expect(sessionListActions.setActiveSession).toHaveBeenCalledWith('th-nav')

    unsubscribe()
    expect(disposer).toHaveBeenCalledTimes(1)
  })

  it('Web（无 electronAPI）与旧版壳（无 onNotificationNavigate 通道）→ no-op 退订函数不抛错', () => {
    expect(() => installNotificationNavigateListener()).not.toThrow()
    expect(installNotificationNavigateListener()()).toBeUndefined()

    setBridge({ show: vi.fn(async () => true) })
    expect(() => installNotificationNavigateListener()()).not.toThrow()
    expect(sessionListActions.setActiveSession).not.toHaveBeenCalled()
  })
})
