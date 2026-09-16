/**
 * agentTab 测试家族共享的 pipelineMessageStore mock 工厂。
 *
 * 三个测试文件的 vi.mock 工厂曾逐文件复制（jscpd 克隆门禁最大重复源之一）；
 * 统一为超集形态：含 getMessages/activePipelineId，未用这些键的文件不受影响。
 * 调用方式（工厂内异步 import，规避 vi.mock 提升引用限制）：
 *   vi.mock('@/stores/pipelineMessageStore', async () =>
 *     (await import('./helpers/pipelineStoreMockFactory')).makePipelineStoreMock())
 */
import { vi } from 'vitest'

export function makePipelineStoreMock() {
  const state = {
    activatePipeline: vi.fn(),
    registerPipeline: vi.fn(),
    loadPipelineMessages: vi.fn(() => Promise.resolve({ ok: true as const })),
    getMessages: vi.fn(() => [] as unknown[]),
    pipelines: {} as Record<string, unknown>,
    messagesByPipeline: {} as Record<string, unknown[]>,
    activePipelineId: null as string | null,
  }
  // 与真实 store 同语义：激活回写 activePipelineId
  state.activatePipeline = vi.fn((pipelineId: string) => {
    state.activePipelineId = pipelineId
  })
  const setState = vi.fn((partial: Record<string, unknown>) => {
    Object.assign(state, partial)
  })
  return {
    usePipelineMessageStore: { getState: () => state, setState },
    __pipelineMockState: state,
  }
}
