// @feature: FP-0.2.四 前端Schema | @ci: frontend-test
/**
 * PendingInputQueueBar 组件测试
 *
 * 覆盖：快照引用稳定性（空队列不触发 useSyncExternalStore 无限循环）、
 * 空队列零渲染、恒常平铺（无折叠，全文显示不省略）、三个显式操作按钮
 * （回到输入框/立即发送/删除，无需先展开）、退回/删除/清空的出队行为。
 * 排队条目是排队期唯一视图（2026-10-26 用户裁定）：气泡在引擎消费
 * （consumed 事件认领链补插）时才出现，队列条不承担气泡同步职责。
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { PendingInputQueueBar } from '@/components/chat/PendingInputQueueBar'
import { useChatInputStore } from '@/stores/chatInputStore'
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
  source: 'user' | 'trigger' = 'user',
  cmid?: string,
): PendingInputItem => ({
  id,
  pipeline_id: 'pipe-1',
  content,
  source,
  created_at: '2026-08-26T01:00:00Z',
  ...(cmid ? { client_message_id: cmid } : {}),
})

describe('PendingInputQueueBar', () => {
  beforeEach(async () => {
    // clearAllMocks 不重置 mockImplementation——先清调用历史，再落回默认实现，
    // 防前一用例的内核模拟列表（闭包可变数组）泄漏到后续用例的对账拉取
    const api = await import('@/services/api/pipelines')
    vi.clearAllMocks()
    vi.mocked(api.fetchPendingInputs).mockResolvedValue([])
    vi.mocked(api.deletePendingInput).mockResolvedValue(undefined as never)
    usePendingInputStore.setState({ byPipeline: {} })
    useChatInputStore.setState({ pendingInsert: null })
  })

  it('空队列：渲染不抛 Maximum update depth（快照引用稳定）且零渲染', () => {
    expect(() => render(<PendingInputQueueBar pipelineId="pipe-1" />)).not.toThrow()
    expect(screen.queryByTestId('pending-queue-bar')).toBeNull()
  })

  it('恒常平铺：所有条目全文显示，无折叠（不再省略后文）', () => {
    usePendingInputStore.getState().syncFromEvent('pipe-1', [
      item('a', '第一条完整内容不说半句', 'user', 'cmid-a'),
      item('b', '第二条全文也直接可见', 'trigger'),
    ])
    render(<PendingInputQueueBar pipelineId="pipe-1" />)
    expect(screen.getByTestId('pending-queue-bar')).toBeInTheDocument()
    expect(screen.getByText('2 条待处理')).toBeInTheDocument()
    // 无「展开」交互：条目内容直接全文呈现（非截断预览）
    expect(screen.getByText('第一条完整内容不说半句')).toBeInTheDocument()
    expect(screen.getByText('第二条全文也直接可见')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /条待处理/ })).toBeNull()
  })

  it('三个操作按钮无需展开即可见：回到输入框 / 立即发送 / 删除本条', () => {
    usePendingInputStore
      .getState()
      .syncFromEvent('pipe-1', [item('a', '第一条', 'user', 'cmid-a')])
    const onForceSend = vi.fn()
    render(<PendingInputQueueBar pipelineId="pipe-1" onForceSend={onForceSend} />)
    expect(screen.getByRole('button', { name: '回到输入框修改' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '立即发送第 1 条' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '删除本条' })).toBeInTheDocument()
    // 立即发送直接可点（不再「点一下才出现」）
    fireEvent.click(screen.getByRole('button', { name: '立即发送第 1 条' }))
    expect(onForceSend).toHaveBeenCalledWith('a')
  })

  it('回到输入框：该条出队回填输入框（与停止退回同通道）', async () => {
    // F10 对账钩子挂载即拉内核权威列表：mock 模拟真实内核——delete 真实改列表，
    // 任何 load/删除时序交错下结果都收敛到同一终态
    const api = await import('@/services/api/pipelines')
    const kernelList = [
      item('a', '第一条', 'user', 'cmid-a'),
      item('b', '第二条', 'user', 'cmid-b'),
    ]
    vi.mocked(api.fetchPendingInputs).mockImplementation(async () => [...kernelList])
    vi.mocked(api.deletePendingInput).mockImplementation(async (_pid: string, id: string) => {
      const i = kernelList.findIndex((x) => x.id === id)
      if (i >= 0) kernelList.splice(i, 1)
      return undefined as never
    })
    usePendingInputStore
      .getState()
      .syncFromEvent('pipe-1', [item('a', '第一条', 'user', 'cmid-a'), item('b', '第二条', 'user', 'cmid-b')])
    render(<PendingInputQueueBar pipelineId="pipe-1" />)
    await waitFor(() => {
      expect(usePendingInputStore.getState().byPipeline['pipe-1']).toHaveLength(2)
    })
    fireEvent.click(screen.getAllByRole('button', { name: '回到输入框修改' })[0])

    // 内容进入输入框桥接（与停止退回同通道）
    expect(useChatInputStore.getState().pendingInsert).toBe('第一条')
    // 条目从队列删除（先出队再改再发）
    expect(api.deletePendingInput).toHaveBeenCalledWith('pipe-1', 'a')
    await waitFor(() => {
      expect(usePendingInputStore.getState().byPipeline['pipe-1']).toHaveLength(1)
    })
  })

  it('删除本条：出队', async () => {
    const api = await import('@/services/api/pipelines')
    usePendingInputStore
      .getState()
      .syncFromEvent('pipe-1', [item('a', '第一条', 'user', 'cmid-a')])
    render(<PendingInputQueueBar pipelineId="pipe-1" />)
    fireEvent.click(screen.getByRole('button', { name: '删除本条' }))
    await waitFor(() => {
      expect(api.deletePendingInput).toHaveBeenCalledWith('pipe-1', 'a')
      expect(usePendingInputStore.getState().byPipeline['pipe-1']).toHaveLength(0)
    })
  })

  it('清空队列：全部出队（DELETE 全量端点）', async () => {
    const api = await import('@/services/api/pipelines')
    usePendingInputStore.getState().syncFromEvent('pipe-1', [
      item('a', '第一条', 'user', 'cmid-a'),
      item('b', '第二条', 'user', 'cmid-b'),
    ])
    render(<PendingInputQueueBar pipelineId="pipe-1" />)
    fireEvent.click(screen.getByRole('button', { name: '清空队列' }))
    await waitFor(() => {
      expect(api.clearPendingInputs).toHaveBeenCalledWith('pipe-1')
      expect(usePendingInputStore.getState().byPipeline['pipe-1']).toHaveLength(0)
    })
  })

  it('删除失败：条目留在队列（对账收敛，无未处理拒绝崩溃）', async () => {
    const api = await import('@/services/api/pipelines')
    // mock 自洽：DELETE 失败 = 条目仍在内核队列，GET 对账权威列表同样返回该条
    vi.mocked(api.deletePendingInput).mockRejectedValueOnce(new Error('network down'))
    vi.mocked(api.fetchPendingInputs).mockResolvedValue([item('a', '第一条', 'user', 'cmid-a')])
    usePendingInputStore
      .getState()
      .syncFromEvent('pipe-1', [item('a', '第一条', 'user', 'cmid-a')])
    render(<PendingInputQueueBar pipelineId="pipe-1" />)
    fireEvent.click(screen.getByRole('button', { name: '删除本条' }))
    await waitFor(() => {
      expect(api.deletePendingInput).toHaveBeenCalled()
    })
    // 等一个微任务让 rejection 路径走完：条目保留、无未处理拒绝崩溃
    await new Promise((r) => setTimeout(r, 0))
    expect(usePendingInputStore.getState().byPipeline['pipe-1']).toHaveLength(1)
  })
})
