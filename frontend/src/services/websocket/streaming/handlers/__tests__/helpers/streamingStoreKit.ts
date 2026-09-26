/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * handler 流式测试家族共享的 store 装配：重置模块图、清空 pipelineMessageStore
 * 各状态桶并挂 window.__pipelineStore（handlers 内部消费面），注册当前管道。
 * activePipelineId 缺省 null（个别场景传 pipelineId 模拟已激活流）。
 */
import { vi } from 'vitest'

export async function resetStreamingStore(
  pipelineId: string,
  threadId: string,
  activePipelineId: string | null = null,
) {
  vi.resetModules()
  const storeMod = await import('@/stores/pipelineMessageStore')
  const store = storeMod.usePipelineMessageStore
  ;(window as any).__pipelineStore = store
  store.setState({
    messagesByPipeline: {},
    pipelines: {},
    pipelineSessionMap: { [pipelineId]: threadId },
    streamingState: {},
    activePipelineId,
    topCursorsByPipeline: {},
    bottomCursorsByPipeline: {},
    hasMoreOlderByPipeline: {},
    isLoadingOlderByPipeline: {},
  })
  store.getState().registerPipeline({
    pipelineId,
    sessionId: threadId,
    level: 1,
    tabId: null,
    agentName: '',
    status: 'idle',
    parentId: null,
    unreadCount: 0,
  } as any)
  return store
}
