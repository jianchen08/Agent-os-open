/** @feature 交互裁定 2026-09-02 | @ci frontend-test */
/**
 * interactionStore.getPendingConversationsForPipeline 测试
 *
 * 2026-09-02 用户裁定：对交互工具，用户发送消息本身即一次响应——解除挂起、
 * 工具返回空回复、消息继续推进。发送链路据此对目标管道的未决 conversation
 * 交互逐条提交空 approved。
 *
 * 匹配规则：
 * - status ∈ {pending, entered}（未点进入对话与已进入对话均算未决）
 * - mode === 'conversation'（choice 需显式选选项、notification 不阻塞，均不解除）
 * - 归属同 getEnteredForPipeline：pipelineId 精确命中 / 缺失时 threadId 兼容
 * - 同管道多条全部返回（用户发消息 == 明确解除意图，不做歧义豁免）
 */
import { beforeEach, describe, expect, it } from 'vitest'
import { useInteractionStore } from '@/stores/interactionStore'
import type { PendingInteraction } from '@/stores/interactionStore'

function conv(overrides: Partial<PendingInteraction>): PendingInteraction {
  return {
    requestId: 'req-1',
    mode: 'conversation',
    title: '对话',
    description: '',
    threadId: 'thread-A',
    tabId: 'tab-1',
    agentId: 'agent-X',
    timestamp: '2026-08-22T00:00:00.000Z',
    status: 'pending',
    ...overrides,
  }
}

/** addInteraction 强制 status=pending，注入其他状态须走 setState */
function seed(...items: PendingInteraction[]) {
  useInteractionStore.setState({ pendingInteractions: items })
}

beforeEach(() => {
  useInteractionStore.setState({ pendingInteractions: [] })
})

describe('getPendingConversationsForPipeline 精确归属', () => {
  it('pipelineId 精确命中 pending conversation 交互', () => {
    seed(conv({ requestId: 'a', pipelineId: 'P1' }))
    const hits = useInteractionStore.getState().getPendingConversationsForPipeline('P1')
    expect(hits.map((h) => h.requestId)).toEqual(['a'])
  })

  it('entered 状态同样匹配（已点进入对话、管道挂起等用户输入）', () => {
    seed(conv({ requestId: 'a', pipelineId: 'P1', status: 'entered' }))
    const hits = useInteractionStore.getState().getPendingConversationsForPipeline('P1')
    expect(hits.map((h) => h.requestId)).toEqual(['a'])
  })

  it('choice 模式不参与（需用户显式选选项）', () => {
    seed(conv({ requestId: 'a', pipelineId: 'P1', mode: 'choice' as const }))
    const hits = useInteractionStore.getState().getPendingConversationsForPipeline('P1')
    expect(hits).toEqual([])
  })

  it('notification 模式不参与（不阻塞管道）', () => {
    seed(conv({ requestId: 'a', pipelineId: 'P1', mode: 'notification' as const }))
    const hits = useInteractionStore.getState().getPendingConversationsForPipeline('P1')
    expect(hits).toEqual([])
  })

  it('已响应（responded）不参与', () => {
    seed(conv({ requestId: 'a', pipelineId: 'P1', status: 'responded' as const }))
    const hits = useInteractionStore.getState().getPendingConversationsForPipeline('P1')
    expect(hits).toEqual([])
  })

  it('pipelineId 缺失时允许 threadId 命中（旧命名空间兼容，同 getEnteredForPipeline）', () => {
    seed(conv({ requestId: 'a', pipelineId: undefined, threadId: 'P1' }))
    const hits = useInteractionStore.getState().getPendingConversationsForPipeline('P1')
    expect(hits.map((h) => h.requestId)).toEqual(['a'])
  })

  it('pipelineId 在场时不跨命名空间用 threadId 撞键', () => {
    seed(conv({ requestId: 'a', pipelineId: 'P2', threadId: 'P1' }))
    const hits = useInteractionStore.getState().getPendingConversationsForPipeline('P1')
    expect(hits).toEqual([])
  })

  it('agentId 绝不参与管道归属匹配', () => {
    seed(conv({ requestId: 'a', pipelineId: undefined, threadId: 'T1', agentId: 'P1' }))
    const hits = useInteractionStore.getState().getPendingConversationsForPipeline('P1')
    expect(hits).toEqual([])
  })

  it('同管道多条未决 conversation 全部返回（用户发消息 == 逐一解除）', () => {
    seed(
      conv({ requestId: 'a', pipelineId: 'P1' }),
      conv({ requestId: 'b', pipelineId: 'P1', status: 'entered' as const }),
    )
    const hits = useInteractionStore.getState().getPendingConversationsForPipeline('P1')
    expect(hits.map((h) => h.requestId).sort()).toEqual(['a', 'b'])
  })

  it('他管道交互不误中（会话坐标不跨管道）', () => {
    seed(conv({ requestId: 'a', pipelineId: 'P1' }))
    const hits = useInteractionStore.getState().getPendingConversationsForPipeline('P2')
    expect(hits).toEqual([])
  })
})