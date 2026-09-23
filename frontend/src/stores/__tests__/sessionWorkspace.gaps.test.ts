// @feature FP-T12 补测 | @ci frontend-test
/**
 * store 分支补遗批：
 * - sessionStore：重连时旧 WS 订阅先 cleanup 再重连（connectWebSocket 幂等）
 * - workspaceStore：_normalizeWorkspace 对 id 缺失载荷的契约外输入防御（console.error + 空串回退）
 *
 * 外部依赖（globalWS / API 层）mock；store 本体真实。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'

const { mockWsConnect } = vi.hoisted(() => ({ mockWsConnect: vi.fn() }))
vi.mock('@/services/websocket/GlobalWebSocket', () => ({
  globalWS: {
    connect: mockWsConnect,
    status: 'connecting',
    on: vi.fn(),
    off: vi.fn(),
  },
}))

const { mockGetWorkspace } = vi.hoisted(() => ({ mockGetWorkspace: vi.fn() }))
vi.mock('@/services/api/workspaces', async (importOriginal) => {
  const mod = await importOriginal<typeof import('@/services/api/workspaces')>()
  return { ...mod, getWorkspace: (...args: unknown[]) => mockGetWorkspace(...args) }
})

import { useSessionStore } from '../sessionStore'
import { useWorkspaceStore } from '../workspaceStore'

describe('sessionStore — 重连清理', () => {
  it('已有旧订阅时 connectWebSocket 先 cleanup 旧订阅再重连（33 行分支）', () => {
    const cleanup = vi.fn()
    useSessionStore.setState({ _wsUnsubscribers: { cleanup } as never })

    useSessionStore.getState().connectWebSocket('token-1')

    expect(cleanup).toHaveBeenCalledTimes(1)
    expect(mockWsConnect).toHaveBeenCalledWith('token-1')
    expect(useSessionStore.getState().wsStatus).toBe('connecting')
  })

  it('无旧订阅（首连）→ 不调 cleanup，直接连接', () => {
    useSessionStore.setState({ _wsUnsubscribers: null })
    useSessionStore.getState().connectWebSocket('token-2')
    expect(mockWsConnect).toHaveBeenCalledWith('token-2')
  })
})

describe('workspaceStore — _normalizeWorkspace id 缺失防御', () => {
  beforeEach(() => {
    mockGetWorkspace.mockReset()
    useWorkspaceStore.setState({ workspaces: {}, loading: false, error: null } as never)
  })

  it('载荷缺 id（契约外）→ console.error 且 id 落空串，不抛错不中断', async () => {
    const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {})
    mockGetWorkspace.mockResolvedValue({
      containerTaskId: 'c1',
      title: '无 id 的载荷',
      sessionId: 's1',
    })

    const ws = await useWorkspaceStore.getState().fetchWorkspace('c1')

    expect(errSpy).toHaveBeenCalledWith(
      expect.stringContaining('_normalizeWorkspace'),
      expect.objectContaining({ containerTaskId: 'c1' }),
    )
    expect(ws?.id).toBe('')
    expect(ws?.title).toBe('无 id 的载荷')
    errSpy.mockRestore()
  })

  it('正常载荷（有 id）→ 不触发 console.error（防误报回归）', async () => {
    const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {})
    mockGetWorkspace.mockResolvedValue({
      id: 'ws-real-id',
      containerTaskId: 'c2',
      title: '正常',
      sessionId: 's1',
      fileTree: [],
      createdAt: '',
      updatedAt: '',
    })

    const ws = await useWorkspaceStore.getState().fetchWorkspace('c2')

    expect(errSpy).not.toHaveBeenCalled()
    expect(ws?.id).toBe('ws-real-id')
    errSpy.mockRestore()
  })
})
