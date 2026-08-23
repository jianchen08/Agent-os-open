/**
 * interactionStore.getEnteredForPipeline 精确归属测试（2026-08-22 裁决）
 *
 * 反模式收口：旧实现"跨 ID 命名空间撞键"——pipelineId/threadId/agentId 三个
 * 不同实体坐标对探针键做析取匹配 + find 取第一条，同字符串撞键时把别的
 * 交互的请求自动批准掉；且列表按优先级/时间排序，命中对象与身份无关。
 *
 * 新规则：pipelineId 精确命中优先；pipelineId 缺失时允许 threadId 命中
 * （thread==pipeline 旧身份命名空间的确定性兼容）；agentId 绝不参与管道
 * 归属；多命中（同管道多条 entered）→ 不返回（不自动批准，暴露歧义）。
 */
import { beforeEach, describe, expect, it } from 'vitest'
import { useInteractionStore } from '@/stores/interactionStore'
import type { PendingInteraction } from '@/stores/interactionStore'

function entered(overrides: Partial<PendingInteraction>): PendingInteraction {
  return {
    requestId: 'req-1',
    mode: 'choice',
    title: '审批',
    description: '',
    threadId: 'thread-A',
    tabId: 'tab-1',
    agentId: 'agent-X',
    timestamp: '2026-08-22T00:00:00.000Z',
    status: 'entered',
    ...overrides,
  }
}

/** addInteraction 强制 status=pending，注入 entered 状态须走 setState */
function seed(...items: PendingInteraction[]) {
  useInteractionStore.setState({ pendingInteractions: items })
}

beforeEach(() => {
  useInteractionStore.setState({ pendingInteractions: [] })
})

describe('getEnteredForPipeline 精确归属', () => {
  it('pipelineId 精确命中优先——另一条交互的 agentId 撞键不顶替（旧实现 find 取第一条会错批）', () => {
    seed(
      entered({ requestId: 'a', pipelineId: 'P1', threadId: 'T1', timestamp: '2026-08-22T01:00:00.000Z' }),
      // agentId 恰为 P1 的另一条 entered（排序在前）——旧实现会命中它并自动批准
      entered({
        requestId: 'b',
        pipelineId: 'P2',
        threadId: 'T2',
        agentId: 'P1',
        timestamp: '2026-08-22T00:00:00.000Z',
      }),
    )
    const hit = useInteractionStore.getState().getEnteredForPipeline('P1')
    expect(hit?.requestId).toBe('a')
  })

  it('pipelineId 缺失时允许 threadId 命中（thread==pipeline 旧命名空间兼容）', () => {
    seed(entered({ requestId: 'a', pipelineId: undefined, threadId: 'P1' }))
    const hit = useInteractionStore.getState().getEnteredForPipeline('P1')
    expect(hit?.requestId).toBe('a')
  })

  it('pipelineId 在场时不跨命名空间用 threadId 撞键', () => {
    seed(entered({ requestId: 'a', pipelineId: 'P2', threadId: 'P1' }))
    const hit = useInteractionStore.getState().getEnteredForPipeline('P1')
    expect(hit).toBeUndefined()
  })

  it('agentId 绝不参与管道归属匹配', () => {
    seed(entered({ requestId: 'a', pipelineId: undefined, threadId: 'T1', agentId: 'P1' }))
    const hit = useInteractionStore.getState().getEnteredForPipeline('P1')
    expect(hit).toBeUndefined()
  })

  it('同管道多条 entered → 不返回（歧义不自动批准，宁可暴露）', () => {
    seed(
      entered({ requestId: 'a', pipelineId: 'P1' }),
      entered({ requestId: 'b', pipelineId: 'P1' }),
    )
    const hit = useInteractionStore.getState().getEnteredForPipeline('P1')
    expect(hit).toBeUndefined()
  })

  it('非 entered 状态（pending）不参与匹配', () => {
    seed(entered({ requestId: 'a', pipelineId: 'P1', status: 'pending' }))
    const hit = useInteractionStore.getState().getEnteredForPipeline('P1')
    expect(hit).toBeUndefined()
  })
})