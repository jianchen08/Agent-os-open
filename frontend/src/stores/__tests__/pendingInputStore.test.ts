/**
 * pending 输入队列 store 测试（ADR-2026-08-26）
 *
 * 覆盖：WS 事件同步（enqueued/consumed）、PUT 修改乐观更新、删除/清空、
 * 编辑态清理（条目消失时）。
 */
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { usePendingInputStore } from '@/stores/pendingInputStore'
import * as api from '@/services/api/pipelines'

vi.mock('@/services/api/pipelines', async (importOriginal) => {
  const actual = await importOriginal<typeof api>()
  return {
    ...actual,
    updatePendingInput: vi.fn().mockResolvedValue(undefined),
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
    usePendingInputStore.setState({ byPipeline: {}, editingId: {} })
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

  it('updateContent：调用 PUT 并乐观更新（不改变 FIFO 位置）', async () => {
    usePendingInputStore.getState().syncFromEvent('pipe-1', [item('a', '旧内容'), item('b', '第二条')])
    await usePendingInputStore.getState().updateContent('pipe-1', 'a', '新内容')
    expect(api.updatePendingInput).toHaveBeenCalledWith('pipe-1', 'a', '新内容')
    const items = usePendingInputStore.getState().byPipeline['pipe-1']
    expect(items[0].content).toBe('新内容')
    expect(items[0].id).toBe('a')
    expect(usePendingInputStore.getState().editingId['pipe-1']).toBeNull()
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

  it('setEditing 与消费后编辑态清理（条目消失不再残留）', () => {
    const s = usePendingInputStore.getState()
    s.syncFromEvent('pipe-1', [item('a', 'x')])
    s.setEditing('pipe-1', 'a')
    expect(usePendingInputStore.getState().editingId['pipe-1']).toBe('a')
    // 事件回推条目已消费 → 编辑态清空
    s.syncFromEvent('pipe-1', [])
    expect(usePendingInputStore.getState().editingId['pipe-1']).toBeNull()
  })

  it('load：GET 拉取对账（刷新恢复）', async () => {
    vi.mocked(api.fetchPendingInputs).mockResolvedValueOnce([item('a', 'x'), item('b', 'y')])
    await usePendingInputStore.getState().load('pipe-1')
    expect(api.fetchPendingInputs).toHaveBeenCalledWith('pipe-1')
    expect(usePendingInputStore.getState().byPipeline['pipe-1']).toHaveLength(2)
  })
})
