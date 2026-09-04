// @feature: FP-0.2.四 前端Schema | @ci: frontend-test
/**
 * PendingInputQueueBar 组件测试
 *
 * 覆盖：快照引用稳定性（空队列不触发 useSyncExternalStore 无限循环）、
 * 空队列零渲染、有队列时的可见行为（条数/首条预览）。
 */
import { fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { PendingInputQueueBar } from '@/components/chat/PendingInputQueueBar'
import { usePendingInputStore } from '@/stores/pendingInputStore'
import type { PendingInputItem } from '@/services/api/pipelines'
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

const item = (id: string, content: string, source: 'user' | 'trigger' = 'user'): PendingInputItem => ({
  id,
  pipeline_id: 'pipe-1',
  content,
  source,
  created_at: '2026-08-26T01:00:00Z',
})

describe('PendingInputQueueBar', () => {
  beforeEach(() => {
    usePendingInputStore.setState({ byPipeline: {}, editingId: {} })
    vi.clearAllMocks()
  })

  it('空队列：渲染不抛 Maximum update depth（快照引用稳定）且零渲染', () => {
    expect(() => render(<PendingInputQueueBar pipelineId="pipe-1" />)).not.toThrow()
    expect(screen.queryByTestId('pending-queue-bar')).toBeNull()
  })

  it('有队列：显示条数与首条预览', () => {
    usePendingInputStore
      .getState()
      .syncFromEvent('pipe-1', [item('a', '第一条'), item('b', '第二条', 'trigger')])
    render(<PendingInputQueueBar pipelineId="pipe-1" />)
    expect(screen.getByTestId('pending-queue-bar')).toBeInTheDocument()
    expect(screen.getByText('2 条待处理')).toBeInTheDocument()
    expect(screen.getByText('第一条')).toBeInTheDocument()
  })

  it('展开/编辑时首条内容只渲染一次（头部预览与列表行不同时出现）', () => {
    usePendingInputStore
      .getState()
      .syncFromEvent('pipe-1', [item('a', '第一条'), item('b', '第二条', 'trigger')])
    render(<PendingInputQueueBar pipelineId="pipe-1" />)
    // 收起态：头部预览是首条内容的唯一渲染
    expect(screen.getAllByText('第一条')).toHaveLength(1)
    // 展开后列表逐条呈现，头部不再重复渲染首条内容
    fireEvent.click(screen.getByRole('button', { name: /2 条待处理/ }))
    expect(screen.getAllByText('第一条')).toHaveLength(1)
    // 进入编辑：内容只存在于编辑输入框，不再有任何文本节点重复渲染
    fireEvent.click(screen.getAllByTitle('点击修改')[0])
    expect(screen.getByDisplayValue('第一条')).toBeInTheDocument()
    expect(screen.queryAllByText('第一条')).toHaveLength(0)
    // 取消修改：退出编辑态，条目恢复原内容渲染
    fireEvent.click(screen.getByRole('button', { name: '取消修改' }))
    expect(screen.getByText('第一条')).toBeInTheDocument()
    expect(screen.queryByDisplayValue('第一条')).toBeNull()
  })
})
