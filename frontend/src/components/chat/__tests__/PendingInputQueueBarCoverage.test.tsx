/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * PendingInputQueueBar 覆盖缺口补测（与 PendingInputQueueBar.test.tsx 互补，不重复）
 *
 * 覆盖契约：
 * - 展开列表逐条呈现：序号 / 来源标签（user→人、trigger→触发器、未知→原样）、
 *   时间列、删除本条（仅调对应条目）
 * - 编辑态：Enter 保存（调 updateContent 且退出编辑态）、Escape 取消（不提交）、
 *   空白内容保存为 no-op（不提交），保存按钮等价 Enter
 * - 清空队列按钮调 clear
 * - 含非 user 来源时渲染「含触发器/任务注入」提示；全 user 时不渲染
 *
 * 测试策略：真实 store（zustand 真实实现）+ 真实组件；仅 mock API 层（外部依赖）。
 */

import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { PendingInputQueueBar } from '@/components/chat/PendingInputQueueBar'
import { usePendingInputStore } from '@/stores/pendingInputStore'
import type * as api from '@/services/api/pipelines'
import type { PendingInputItem } from '@/services/api/pipelines'

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

const item = (
  id: string,
  content: string,
  source = 'user',
): PendingInputItem => ({
  id,
  pipeline_id: 'pipe-1',
  content,
  source,
  created_at: '2026-08-26T01:00:00Z',
})

function seed(items: PendingInputItem[], pipelineId = 'pipe-1') {
  usePendingInputStore.getState().syncFromEvent(pipelineId, items)
}

function expandList() {
  fireEvent.click(screen.getByRole('button', { name: /条待处理/ }))
}

describe('PendingInputQueueBar — 展开列表与来源标注', () => {
  beforeEach(() => {
    usePendingInputStore.setState({ byPipeline: {}, editingId: {} })
    vi.clearAllMocks()
  })

  it('展开后逐条渲染：序号、已知来源标签、未知来源原样展示', () => {
    seed([item('a', '第一条', 'user'), item('b', '第二条', 'trigger'), item('c', '第三条', 'webhook')])
    render(<PendingInputQueueBar pipelineId="pipe-1" />)
    expandList()

    const rows = screen.getByTestId('pending-queue-list').querySelectorAll('li')
    expect(rows).toHaveLength(3)
    expect(rows[0].textContent).toContain('1')
    expect(rows[0].textContent).toContain('人')
    expect(rows[1].textContent).toContain('触发器')
    // 未在 SOURCE_LABELS 词表中的来源原样透出
    expect(rows[2].textContent).toContain('webhook')
  })

  it('展开列表同时显示每条内容与时间列', () => {
    seed([item('a', '内容甲'), item('b', '内容乙')])
    render(<PendingInputQueueBar pipelineId="pipe-1" />)
    expandList()
    expect(screen.getByText('内容甲')).toBeInTheDocument()
    expect(screen.getByText('内容乙')).toBeInTheDocument()
    // 时间为 toLocaleTimeString 渲染（只断言存在非空时间文本节点）
    expect(screen.getByTestId('pending-queue-list').textContent).toMatch(/\d{1,2}:\d{2}/)
  })

  it('删除本条只移除对应条目（其余保留）', async () => {
    seed([item('a', '内容甲'), item('b', '内容乙')])
    render(<PendingInputQueueBar pipelineId="pipe-1" />)
    expandList()

    fireEvent.click(screen.getAllByRole('button', { name: '删除本条' })[0])
    await waitFor(() => {
      expect(screen.queryByText('内容甲')).toBeNull()
    })
    expect(screen.getByText('内容乙')).toBeInTheDocument()
  })

  it('清空队列移除所有条目并回到零渲染', async () => {
    seed([item('a', '内容甲'), item('b', '内容乙')])
    render(<PendingInputQueueBar pipelineId="pipe-1" />)
    expect(screen.getByTestId('pending-queue-bar')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '清空队列' }))
    await waitFor(() => {
      expect(screen.queryByTestId('pending-queue-bar')).toBeNull()
    })
  })

  it('含非 user 来源时提示触发器/任务注入，全 user 时不提示', () => {
    seed([item('a', '甲', 'user')])
    const { unmount } = render(<PendingInputQueueBar pipelineId="pipe-1" />)
    expect(screen.queryByText('含触发器/任务注入')).toBeNull()
    unmount()

    seed([item('a', '甲', 'task')])
    render(<PendingInputQueueBar pipelineId="pipe-1" />)
    expect(screen.getByText('含触发器/任务注入')).toBeInTheDocument()
  })
})

describe('PendingInputQueueBar — 编辑态', () => {
  beforeEach(() => {
    usePendingInputStore.setState({ byPipeline: {}, editingId: {} })
    vi.clearAllMocks()
  })

  it('Enter 保存修改：内容更新且退出编辑态', async () => {
    seed([item('a', '原文'), item('b', '其他')])
    render(<PendingInputQueueBar pipelineId="pipe-1" />)
    expandList()
    fireEvent.click(screen.getAllByTitle('点击修改')[0])

    const input = screen.getByLabelText('编辑第 1 条')
    fireEvent.change(input, { target: { value: '改后' } })
    fireEvent.keyDown(input, { key: 'Enter' })

    await waitFor(() => {
      expect(screen.getByText('改后')).toBeInTheDocument()
    })
    expect(screen.queryByDisplayValue('改后')).toBeNull()
  })

  it('保存按钮与 Enter 等价（点击保存同样提交并退出编辑态）', async () => {
    seed([item('a', '原文')])
    render(<PendingInputQueueBar pipelineId="pipe-1" />)
    expandList()
    fireEvent.click(screen.getAllByTitle('点击修改')[0])
    fireEvent.change(screen.getByLabelText('编辑第 1 条'), { target: { value: '按钮保存' } })
    fireEvent.click(screen.getByRole('button', { name: '保存修改' }))

    await waitFor(() => {
      expect(screen.getByText('按钮保存')).toBeInTheDocument()
    })
  })

  it('Escape 取消编辑：内容保持原值不变', () => {
    seed([item('a', '原文')])
    render(<PendingInputQueueBar pipelineId="pipe-1" />)
    expandList()
    fireEvent.click(screen.getAllByTitle('点击修改')[0])
    const input = screen.getByLabelText('编辑第 1 条')
    fireEvent.change(input, { target: { value: '不应保存' } })
    fireEvent.keyDown(input, { key: 'Escape' })

    expect(screen.getByText('原文')).toBeInTheDocument()
    expect(screen.queryByDisplayValue('不应保存')).toBeNull()
  })

  it('空白内容保存为 no-op：保留原内容且不退出编辑态', () => {
    seed([item('a', '原文')])
    render(<PendingInputQueueBar pipelineId="pipe-1" />)
    expandList()
    fireEvent.click(screen.getAllByTitle('点击修改')[0])
    const input = screen.getByLabelText('编辑第 1 条')
    fireEvent.change(input, { target: { value: '   ' } })
    fireEvent.keyDown(input, { key: 'Enter' })

    // 仍在编辑态（输入框仍存在），原内容未被覆盖
    expect(screen.getByLabelText('编辑第 1 条')).toBeInTheDocument()
    expect(screen.queryByText('原文')).toBeNull()
    expect(usePendingInputStore.getState().byPipeline['pipe-1'][0].content).toBe('原文')
  })
})
