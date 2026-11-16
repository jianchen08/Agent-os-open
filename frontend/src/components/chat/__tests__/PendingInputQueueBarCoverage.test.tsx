/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * PendingInputQueueBar 覆盖缺口补测（与 PendingInputQueueBar.test.tsx 互补，不重复）
 *
 * 覆盖契约（2026-10-24 用户裁定：队列不做折叠，条目恒常平铺全文显示，
 *   三个显式操作按钮直出——原「展开列表」交互退役，本文件相应去展开步骤）：
 * - 列表逐条呈现：序号 / 来源标签（user→人、trigger→触发器、未知→原样）、
 *   时间列、删除本条（仅调对应条目）
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

// F10 对账钩子消费的 seed 快照（vi.hoisted 保证 mock 工厂可用）
const pendingSeedHolder = vi.hoisted(() => ({ items: [] as PendingInputItem[] }))

vi.mock('@/services/api/pipelines', async (importOriginal) => {
  const actual = await importOriginal<typeof api>()
  return {
    ...actual,
    deletePendingInput: vi.fn().mockResolvedValue(undefined),
    clearPendingInputs: vi.fn().mockResolvedValue(undefined),
    // F10 对账钩子挂载即拉权威列表：返回当前 seed 快照（不被空列表覆盖预置条目）
    fetchPendingInputs: vi.fn(async () => pendingSeedHolder.items),
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
  pendingSeedHolder.items = items
  usePendingInputStore.getState().syncFromEvent(pipelineId, items)
}

describe('PendingInputQueueBar — 平铺列表与来源标注', () => {
  beforeEach(() => {
    usePendingInputStore.setState({ byPipeline: {} })
    vi.clearAllMocks()
  })

  it('逐条渲染：序号、已知来源标签、未知来源原样展示（无需展开）', () => {
    seed([item('a', '第一条', 'user'), item('b', '第二条', 'trigger'), item('c', '第三条', 'webhook')])
    render(<PendingInputQueueBar pipelineId="pipe-1" />)

    const rows = screen.getByTestId('pending-queue-list').querySelectorAll('li')
    expect(rows).toHaveLength(3)
    expect(rows[0].textContent).toContain('1')
    expect(rows[0].textContent).toContain('人')
    expect(rows[1].textContent).toContain('触发器')
    // 未在 SOURCE_LABELS 词表中的来源原样透出
    expect(rows[2].textContent).toContain('webhook')
  })

  it('列表同时显示每条内容与时间列', () => {
    seed([item('a', '内容甲'), item('b', '内容乙')])
    render(<PendingInputQueueBar pipelineId="pipe-1" />)
    expect(screen.getByText('内容甲')).toBeInTheDocument()
    expect(screen.getByText('内容乙')).toBeInTheDocument()
    // 时间为 toLocaleTimeString 渲染（只断言存在非空时间文本节点）
    expect(screen.getByTestId('pending-queue-list').textContent).toMatch(/\d{1,2}:\d{2}/)
  })

  it('删除本条只移除对应条目（其余保留）', async () => {
    seed([item('a', '内容甲'), item('b', '内容乙')])
    render(<PendingInputQueueBar pipelineId="pipe-1" />)

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
