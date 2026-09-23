// @feature FP-T12 补测 | @ci frontend-test
/**
 * streamHandler 分支补遗：
 * - stream_start 时活跃 Tab 命中当前管道 → activatePipeline（接管视图）
 * - stream_end 携带 usage → 上下文使用量 store 更新（pull-from-state 数据源）
 *
 * 全部走真实 zustand store，不 mock 内部依赖。
 */
import { beforeEach, describe, expect, it } from 'vitest'
import { useAgentTabStore } from '@/stores/agentTabStore'
import { useContextUsageStore } from '@/stores/contextUsageStore'
import { usePipelineMessageStore } from '@/stores/pipelineMessageStore'
import { usePipelineRegistryStore } from '@/stores/pipelineRegistryStore'
import { handleStreamEnd, handleStreamStart } from '../handlers/streamHandler'

const PIPELINE = 'pipe-a1b2c3d4e5f64789abcdef012345678'

function resetStores(): void {
  usePipelineMessageStore.setState({
    messagesByPipeline: {},
    streamingState: {},
    pipelines: {},
    pipelineSessionMap: {},
    activePipelineId: null,
  } as never)
  useAgentTabStore.setState({ tabs: [], activeTabId: null } as never)
  usePipelineRegistryStore.setState({ runsByPipeline: {} } as never)
  useContextUsageStore.setState({ usageByPipeline: {} } as never)
}

describe('handleStreamStart — 活跃 Tab 命中当前管道', () => {
  beforeEach(resetStores)

  it('activeTab.pipelineRunId === pipelineId → activatePipeline 接管（非当前活跃才走此分支）', () => {
    useAgentTabStore.setState({
      tabs: [{ id: 'tab-1', pipelineRunId: PIPELINE } as never],
      activeTabId: 'tab-1',
    } as never)
    expect(usePipelineMessageStore.getState().activePipelineId).toBeNull()

    handleStreamStart({
      data: { pipeline_id: PIPELINE, message_id: 'msg-1' },
      _threadId: 'thread-1',
    } as never)

    expect(usePipelineMessageStore.getState().activePipelineId).toBe(PIPELINE)
    // 占位消息已建（接管前 stream_start 正常建占位）
    expect(
      usePipelineMessageStore.getState().getMessages(PIPELINE).some((m) => m.id === 'msg-1'),
    ).toBe(true)
  })
})

describe('handleStreamEnd — usage 同步', () => {
  beforeEach(resetStores)

  it('事件携带 usage 对象 → updateUsage 落入 contextUsageStore（data.usage 与顶层 usage 两形态）', () => {
    handleStreamEnd({
      data: { pipeline_id: PIPELINE, usage: { prompt_tokens: 120, completion_tokens: 30, total_tokens: 150 } },
    } as never)
    expect(useContextUsageStore.getState().getUsage(PIPELINE)?.promptTokens).toBe(120)

    handleStreamEnd({
      pipeline_id: PIPELINE,
      usage: { input_tokens: 200, output_tokens: 50, total_tokens: 250 },
    } as never)
    expect(useContextUsageStore.getState().getUsage(PIPELINE)?.promptTokens).toBe(200)
    expect(useContextUsageStore.getState().getUsage(PIPELINE)?.totalTokens).toBe(250)
  })

  it('无 usage 字段 → 不写入（保持既有值，不被清空）', () => {
    useContextUsageStore.getState().updateUsage(PIPELINE, { prompt_tokens: 99 })
    handleStreamEnd({ data: { pipeline_id: PIPELINE } } as never)
    expect(useContextUsageStore.getState().getUsage(PIPELINE)?.promptTokens).toBe(99)
  })
})
