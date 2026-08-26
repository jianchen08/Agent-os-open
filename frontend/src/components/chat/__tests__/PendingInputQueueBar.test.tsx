/**
 * PendingInputQueueBar 组件测试
 *
 * 覆盖：快照引用稳定性（空队列不触发 useSyncExternalStore 无限循环）、
 * 空队列零渲染、有队列时的可见行为（条数/首条预览）。
 */
import { render, screen } from '@testing-library/react'
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
})
