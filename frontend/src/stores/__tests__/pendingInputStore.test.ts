/** @feature FP-0.2.待发送队列 待发送输入 store | @ci: frontend-test */
/**
 * pending 输入队列 store 测试（ADR-2026-08-26）
 *
 * 覆盖：WS 事件同步（enqueued/consumed）、删除/清空、GET 对账恢复。
 * （2026-10-19 用户裁定：条目点击=出队退回输入框，编辑态/updateContent 随
 * 条目内编辑功能一并删除。）
 */
import { describe, expect, it, vi, beforeEach } from 'vitest'
import * as api from '@/services/api/pipelines'
import { usePendingInputStore } from '@/stores/pendingInputStore'

vi.mock('@/services/api/pipelines', async (importOriginal) => {
  const actual = await importOriginal<typeof api>()
  return {
    ...actual,
    deletePendingInput: vi.fn().mockResolvedValue(undefined),
    clearPendingInputs: vi.fn().mockResolvedValue(undefined),
    fetchPendingInputs: vi.fn().mockResolvedValue([]),
  }
})

const item = (id: string, content: string, source: 'user' | 'trigger' = 'user') => ({
  id,
  pipeline_id: 'pipe-1',
  content,
  source,
  created_at: '2026-08-26T01:00:00Z',
})

describe('pendingInputStore', () => {
  beforeEach(() => {
    usePendingInputStore.setState({ byPipeline: {} })
    vi.clearAllMocks()
  })

  it('syncFromEvent 覆盖全量列表（入队/消费事件）', () => {
    const s = usePendingInputStore.getState()
    // 入队两条
    s.syncFromEvent('pipe-1', [item('a', '第一条'), item('b', '第二条')])
    expect(usePendingInputStore.getState().byPipeline['pipe-1']).toHaveLength(2)
    // 消费一条（consumed 事件携带剩余全量）
    s.syncFromEvent('pipe-1', [item('b', '第二条')])
    const items = usePendingInputStore.getState().byPipeline['pipe-1']
    expect(items.map((i) => i.id)).toEqual(['b'])
  })

  it('remove/clear 调用对应 API 并更新本地', async () => {
    usePendingInputStore.getState().syncFromEvent('pipe-1', [item('a', 'x'), item('b', 'y')])
    await usePendingInputStore.getState().remove('pipe-1', 'a')
    expect(api.deletePendingInput).toHaveBeenCalledWith('pipe-1', 'a')
    expect(usePendingInputStore.getState().byPipeline['pipe-1']).toHaveLength(1)

    await usePendingInputStore.getState().clear('pipe-1')
    expect(api.clearPendingInputs).toHaveBeenCalledWith('pipe-1')
    expect(usePendingInputStore.getState().byPipeline['pipe-1']).toHaveLength(0)
  })

  it('load：GET 拉取对账（刷新恢复/轮次收尾兜底）', async () => {
    vi.mocked(api.fetchPendingInputs).mockResolvedValueOnce([item('a', 'x'), item('b', 'y')])
    await usePendingInputStore.getState().load('pipe-1')
    expect(api.fetchPendingInputs).toHaveBeenCalledWith('pipe-1')
    expect(usePendingInputStore.getState().byPipeline['pipe-1']).toHaveLength(2)
  })

  it('load：空 pipelineId 不发请求', async () => {
    await usePendingInputStore.getState().load('')
    expect(api.fetchPendingInputs).not.toHaveBeenCalled()
  })
})
