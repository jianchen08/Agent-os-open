// @feature: FP-T12 前端连接层/渲染链路 | @ci: frontend-test
// @feature 交互裁定 2026-09-02 | @ci frontend-test
/**
 * interactionStore.markNavigated / setMinimized 行为测试
 *
 * 契约（状态层可观察行为）：
 * - markNavigated：命中交互 → status='navigated'；未命中/已 navigated → 列表原样不动
 *   （幂等，不新增、不改其他条目）
 * - setMinimized：显式置位与复位（全局浮层最小化开关的可控写面）
 */
import { beforeEach, describe, expect, it } from 'vitest'
import { useInteractionStore } from '@/stores/interactionStore'
import type { PendingInteraction } from '@/stores/interactionStore'

function interaction(overrides: Partial<PendingInteraction>): PendingInteraction {
  return {
    requestId: 'req-1',
    mode: 'choice',
    title: '审批',
    description: '',
    threadId: 'thread-A',
    tabId: 'tab-1',
    agentId: 'agent-X',
    timestamp: '2026-09-01T00:00:00.000Z',
    status: 'pending',
    ...overrides,
  }
}

function seed(...items: PendingInteraction[]) {
  useInteractionStore.setState({ pendingInteractions: items })
}

beforeEach(() => {
  useInteractionStore.setState({
    pendingInteractions: [],
    globalOpenRequestId: null,
    isMinimized: false,
  })
})

describe('markNavigated', () => {
  it('命中交互 → status 置为 navigated，其他条目不受影响', () => {
    seed(
      interaction({ requestId: 'a', title: '甲' }),
      interaction({ requestId: 'b', title: '乙' }),
    )

    useInteractionStore.getState().markNavigated('a')

    const list = useInteractionStore.getState().pendingInteractions
    expect(list.find((i) => i.requestId === 'a')?.status).toBe('navigated')
    expect(list.find((i) => i.requestId === 'b')?.status).toBe('pending')
    expect(list).toHaveLength(2)
  })

  it('已是 navigated → 幂等（第二次调用不再改写，列表引用不变）', () => {
    seed(interaction({ requestId: 'a' }))

    useInteractionStore.getState().markNavigated('a')
    const afterFirst = useInteractionStore.getState().pendingInteractions
    useInteractionStore.getState().markNavigated('a')

    expect(useInteractionStore.getState().pendingInteractions).toBe(afterFirst)
    expect(afterFirst[0].status).toBe('navigated')
  })

  it('requestId 未命中 → 列表与状态原样不动', () => {
    seed(interaction({ requestId: 'a', status: 'pending' }))
    const before = useInteractionStore.getState().pendingInteractions

    useInteractionStore.getState().markNavigated('不存在')

    expect(useInteractionStore.getState().pendingInteractions).toBe(before)
    expect(useInteractionStore.getState().pendingInteractions[0].status).toBe('pending')
  })

  it('responded 条目仍可被标记 navigated（跳转语义独立于响应）', () => {
    seed(interaction({ requestId: 'a', status: 'responded' }))

    useInteractionStore.getState().markNavigated('a')

    expect(useInteractionStore.getState().pendingInteractions[0].status).toBe('navigated')
  })
})

describe('setMinimized', () => {
  it('显式置位与复位（与 toggleMinimized 的翻转语义互补，受控写面）', () => {
    useInteractionStore.getState().setMinimized(true)
    expect(useInteractionStore.getState().isMinimized).toBe(true)

    useInteractionStore.getState().setMinimized(true)
    expect(useInteractionStore.getState().isMinimized).toBe(true)

    useInteractionStore.getState().setMinimized(false)
    expect(useInteractionStore.getState().isMinimized).toBe(false)
  })
})
