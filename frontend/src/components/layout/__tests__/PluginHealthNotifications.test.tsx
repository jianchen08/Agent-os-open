// @feature: FP-0.2.可观测性 插件不可用系统通知（卡3 前端） | @ci: frontend-test
/**
 * PluginHealthNotifications 测试：插件不可用降级 → 通知中心条目（原顶栏横幅
 * 并入右侧实时通知面）的出现/关闭/恢复对账轮询。
 *
 * 清除语义见 pluginHealthStore：轮询既有 contract-status 读面，账本无
 * verify_failed 即清全部；账本为空/读不到保留。
 */
import { act, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
const { mockGet } = vi.hoisted(() => ({ mockGet: vi.fn() }))
vi.mock('@/services/api/client', () => ({ default: { get: mockGet } }))
import {
  resetDismissedNotificationIdsForTest,
  useNotificationStore,
} from '@/stores/notificationStore'
import { usePluginHealthStore } from '@/stores/pluginHealthStore'
import {
  pluginHealthEntryToNotificationInput,
  usePluginHealthNotifications,
  PLUGIN_HEALTH_RECONCILE_INTERVAL_MS,
} from '../PluginHealthNotifications'

function seed(entries: Array<{ key: string; subject: string }>): void {
  usePluginHealthStore.setState({
    unavailable: entries.map((e) => ({
      key: e.key,
      kind: 'verify_failed',
      subject: e.subject,
      content: `${e.subject}当前不可用（根因）；修复插件后将自动恢复，详见插件管理页。`,
      level: 'error',
      firstSeenAt: '2026-10-17T00:00:00.000Z',
    })),
  })
}

function resetNotificationState(): void {
  resetDismissedNotificationIdsForTest()
  useNotificationStore.setState({
    notifications: [],
    isPanelOpen: false,
    activeBlockingNotification: null,
  })
}

beforeEach(() => {
  localStorage.clear()
  usePluginHealthStore.setState({ unavailable: [] })
  resetNotificationState()
  mockGet.mockReset()
})

afterEach(() => {
  vi.useRealTimers()
})

/** 挂载并在 act 内冲刷挂载即对账的微任务（fake timers + act 保证同步落地） */
async function mountAndFlush(): Promise<void> {
  vi.useFakeTimers()
  renderHook(() => usePluginHealthNotifications())
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0)
  })
}

describe('pluginHealthEntryToNotificationInput — 降级项映射为通知条目', () => {
  it('error 级 → critical 红色条目 + 行动指引随正文 + 可关闭（非阻塞）', () => {
    const input = pluginHealthEntryToNotificationInput({
      key: 'k1',
      kind: 'verify_failed',
      subject: '插件 fs_tools',
      content: '插件 fs_tools当前不可用（根因）',
      level: 'error',
      firstSeenAt: '2026-10-17T00:00:00.000Z',
    })
    expect(input.title).toBe('插件 fs_tools不可用')
    expect(input.priority).toBe('critical')
    expect(input.category).toBe('error')
    expect(input.isBlocking).toBe(false)
    expect(input.message).toContain('当前不可用（根因）')
    expect(input.message).toContain('重启应用')
    expect(input.message).toContain('反馈日志')
  })

  it('warn 级 → high 警示条目（优先级分层）；无级（空串）兜底 critical', () => {
    const warn = pluginHealthEntryToNotificationInput({
      key: 'k2',
      kind: 'cohost_member_offline',
      subject: '插件组 light 成员 task_submit',
      content: 'x',
      level: 'warning',
      firstSeenAt: '2026-10-17T00:00:00.000Z',
    })
    expect(warn.priority).toBe('high')

    const unknown = pluginHealthEntryToNotificationInput({
      key: 'k3',
      kind: 'spawn_breaker',
      subject: '插件 a',
      content: 'x',
      level: '',
      firstSeenAt: '2026-10-17T00:00:00.000Z',
    })
    expect(unknown.priority).toBe('critical')
  })
})

describe('usePluginHealthNotifications — 降级项同步为通知中心条目', () => {
  it('有降级项：每条未恢复降级项一条红色条目（正文含哪些插件/组）', async () => {
    seed([
      { key: 'k1', subject: '插件组 light 成员 task_submit' },
      { key: 'k2', subject: '插件 fs_tools' },
    ])
    renderHook(() => usePluginHealthNotifications())
    await waitFor(() => {
      expect(useNotificationStore.getState().notifications).toHaveLength(2)
    })
    const titles = useNotificationStore.getState().notifications.map((n) => n.title)
    expect(titles).toContain('插件组 light 成员 task_submit不可用')
    expect(titles).toContain('插件 fs_tools不可用')
    // 红色错误面：critical + error 分类
    for (const n of useNotificationStore.getState().notifications) {
      expect(n.priority).toBe('critical')
      expect(n.category).toBe('error')
    }
  })

  it('用户关闭条目：本轮不再回弹（同键仍在账本），另一插件新报照常出条目', async () => {
    seed([{ key: 'k1', subject: '插件 a' }])
    renderHook(() => usePluginHealthNotifications())
    await waitFor(() => {
      expect(useNotificationStore.getState().notifications).toHaveLength(1)
    })

    act(() => {
      useNotificationStore.getState().dismissNotification('plugin-health:k1#1')
    })
    // 账本仍在（关闭只影响通知呈现，不改恢复对账）
    expect(usePluginHealthStore.getState().unavailable).toHaveLength(1)

    // 同键新事件（upsert 同账本键）→ 通知不回弹（同 episode id 被关闭记忆挡住）
    act(() => {
      usePluginHealthStore.getState().reportUnavailable({
        key: 'k1',
        kind: 'verify_failed',
        subject: '插件 a',
        content: '插件 a当前不可用（根因更新）',
        level: 'error',
      })
    })
    expect(useNotificationStore.getState().notifications).toHaveLength(0)

    // 另一插件新报 → 照常出条目（只提示新项）
    act(() => {
      usePluginHealthStore.getState().reportUnavailable({
        key: 'k2',
        kind: 'verify_failed',
        subject: '插件 b',
        content: '插件 b当前不可用（根因）',
        level: 'error',
      })
    })
    await waitFor(() => {
      expect(useNotificationStore.getState().notifications).toHaveLength(1)
    })
    expect(useNotificationStore.getState().notifications[0].title).toBe('插件 b不可用')
  })

  it('恢复读面全部转正：条目走解除保留期后自动撤除并清账', async () => {
    seed([{ key: 'k1', subject: '插件 a' }])
    mockGet.mockResolvedValue({
      data: { plugins: [{ plugin_id: 'a', gates: { g2_consistency: 'ok' } }] },
    })
    await mountAndFlush()

    // 账本已清；条目按条件解除保留期延迟撤除
    expect(usePluginHealthStore.getState().unavailable).toHaveLength(0)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(4000)
    })
    expect(useNotificationStore.getState().notifications).toHaveLength(0)
  })

  it('恢复读面仍有 verify_failed：条目保留', async () => {
    seed([{ key: 'k1', subject: '插件 a' }])
    mockGet.mockResolvedValue({
      data: { plugins: [{ plugin_id: 'a', gates: { g2_consistency: 'verify_failed' } }] },
    })
    await mountAndFlush()

    expect(useNotificationStore.getState().notifications).toHaveLength(1)
    expect(usePluginHealthStore.getState().unavailable).toHaveLength(1)
  })

  it('恢复读面不可用（请求失败/账本空）：条目保留，不炸', async () => {
    seed([{ key: 'k1', subject: '插件 a' }])
    mockGet.mockRejectedValue(new Error('network down'))
    await mountAndFlush()

    expect(useNotificationStore.getState().notifications).toHaveLength(1)
    expect(usePluginHealthStore.getState().unavailable).toHaveLength(1)
  })

  it('挂载读不到（首拍失败）后周期对账兜底：读面转正即自动撤', async () => {
    seed([{ key: 'k1', subject: '插件 a' }])
    mockGet.mockRejectedValueOnce(new Error('transient')).mockResolvedValue({
      data: { plugins: [{ plugin_id: 'a', gates: { g2_consistency: 'ok' } }] },
    })
    await mountAndFlush()
    expect(useNotificationStore.getState().notifications).toHaveLength(1)

    // 周期对账转正（清账）→ 条目走解除保留期后撤除
    await act(async () => {
      await vi.advanceTimersByTimeAsync(PLUGIN_HEALTH_RECONCILE_INTERVAL_MS)
    })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(4000)
    })

    expect(useNotificationStore.getState().notifications).toHaveLength(0)
    expect(usePluginHealthStore.getState().unavailable).toHaveLength(0)
    expect(mockGet).toHaveBeenCalledTimes(2)
  })

  it('无降级项不发对账请求（空转零请求）', async () => {
    await mountAndFlush()
    await act(async () => {
      await vi.advanceTimersByTimeAsync(PLUGIN_HEALTH_RECONCILE_INTERVAL_MS * 2)
    })
    expect(mockGet).not.toHaveBeenCalled()
  })
})
