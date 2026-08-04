/**
 * ConversationNavigateTab.test.tsx
 *
 * 回归测试：对话模式点"进入对话"时，跳转标识符必须用 pipelineId（而非 threadId）。
 *
 * 根因：后端 conversation 模式下 thread_id（会话 id）≠ pipeline_id（管道 run id），
 * 而管道标签映射（pipelineTabMap / tabs.pipelineRunId / session.pipelineIds）的 key
 * 全是 pipeline_id。若误传 thread_id，findPipelineLocation 查不到 → 跳转失败。
 */

import { act, fireEvent, render, screen } from '@testing-library/react'
import React from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { InteractionPanel } from '@/components/chat/InteractionPanel'
import type { PendingInteraction } from '@/stores/interactionStore'

// jsdom 不实现 scrollIntoView，InteractionPanel 的滚动 effect 需要它
Element.prototype.scrollIntoView = () => {}

// ---------------------------------------------------------------------------
//  可变 mock 状态：用 vi.hoisted 保证在 vi.mock 提升后仍可引用同一对象
// ---------------------------------------------------------------------------
const mocks = vi.hoisted(() => ({
  pendingInteractions: [] as PendingInteraction[],
  navigateToTab: vi.fn(),
  dismissInteraction: vi.fn(),
}))

vi.mock('@/hooks/useInteractionHandler', () => ({
  useInteractionHandler: () => ({
    pendingInteractions: mocks.pendingInteractions,
    respondChoice: vi.fn(),
    respondConversation: vi.fn(),
    navigateToTab: mocks.navigateToTab,
  }),
}))

// InteractionCard 透传 onNavigateToTab，聚焦被测的调用方逻辑而非卡片渲染
vi.mock('@/components/chat/InteractionCard', () => ({
  InteractionCard: ({ onNavigateToTab }: { onNavigateToTab: () => void }) => (
    <button data-testid="navigate-btn" onClick={onNavigateToTab}>
      进入对话
    </button>
  ),
}))

vi.mock('@/stores/interactionStore', () => ({
  useInteractionStore: (selector: (s: { dismissInteraction: typeof mocks.dismissInteraction }) => unknown) =>
    selector({ dismissInteraction: mocks.dismissInteraction }),
}))

/** 构造 conversation 模式交互 */
function makeConversation(overrides: Partial<PendingInteraction> = {}): PendingInteraction {
  return {
    requestId: 'req-1',
    mode: 'conversation',
    title: '对话',
    description: '',
    threadId: 'thread-1',
    tabId: '',
    agentId: '',
    timestamp: new Date().toISOString(),
    status: 'pending',
    ...overrides,
  }
}

describe('对话模式跳转标识符回归', () => {
  beforeEach(() => {
    mocks.navigateToTab.mockReset()
    mocks.dismissInteraction.mockReset()
    mocks.pendingInteractions = []
  })

  it('pipelineId 存在时应以 pipelineId（而非 threadId）跳转', async () => {
    // 核心回归场景：pipelineId 与 threadId 是不同的值
    mocks.pendingInteractions = [
      makeConversation({ threadId: 'thread-abc', pipelineId: 'pipe-xyz' }),
    ]

    render(<InteractionPanel sessionId="sess-1" />)

    await act(async () => {
      fireEvent.click(screen.getByTestId('navigate-btn'))
    })

    expect(mocks.navigateToTab).toHaveBeenCalledTimes(1)
    // 第 2 个参数是跳转标识符，必须是 pipelineId
    expect(mocks.navigateToTab).toHaveBeenCalledWith(
      'req-1',
      'pipe-xyz',
      '对话',
      undefined,
      undefined,
    )
    // 显式断言：绝不能传成 threadId
    expect(mocks.navigateToTab.mock.calls[0][1]).toBe('pipe-xyz')
    expect(mocks.navigateToTab.mock.calls[0][1]).not.toBe('thread-abc')
  })

  it('pipelineId 缺失（退化场景）时应回退到 threadId', async () => {
    // 退化场景：后端 session_id 缺失时 thread_id 回退成 pipeline_id，二者相等
    mocks.pendingInteractions = [
      makeConversation({ threadId: 'thread-only', pipelineId: undefined }),
    ]

    render(<InteractionPanel sessionId="sess-1" />)

    await act(async () => {
      fireEvent.click(screen.getByTestId('navigate-btn'))
    })

    expect(mocks.navigateToTab).toHaveBeenCalledTimes(1)
    expect(mocks.navigateToTab.mock.calls[0][1]).toBe('thread-only')
  })
})
