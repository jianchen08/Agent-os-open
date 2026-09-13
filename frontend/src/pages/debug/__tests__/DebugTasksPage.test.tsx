// @feature: FP-T12 DebugTasksPage 补测 | @ci: frontend-test
/**
 * DebugTasksPage 调试任务页测试
 *
 * 覆盖：状态词表七态 + 旧值折叠/未知值的渲染分支、行字段投影（标题回退/
 * agent/阶段/关键数据/时间有效性）、状态过滤显式重拉、WS 任务事件订阅与
 * 缓存失效重拉、恢复任务成功/失败、分页翻页与边界禁用。
 *
 * mock 约定：网络层（getTaskList/resumeTask）与 WebSocket 服务为外部依赖，
 * 按 debug 目录既有测试惯例整模块 mock；query 层用全局单例 queryClient
 * （组件 WS 失效走同一实例，重拉行为可用 mock 调用次数观察）。
 */
import { act, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { WS_SERVER_EVENTS } from '@/constants/websocket'
import { getTaskList } from '@/services/api/monitoring'
import { resumeTask } from '@/services/api/tasks'
import type { TaskPauseResumeResponse } from '@/services/api/tasks'
import { queryClient } from '@/services/query/queryClient'
import { globalWS } from '@/services/websocket/GlobalWebSocket'
import { renderWithProviders } from '@/test/renderWithProviders'
import type { TaskInfo } from '@/types/monitoring'
import { DebugTasksPage } from '../DebugTasksPage'

vi.mock('@/services/api/monitoring', () => ({
  getTaskList: vi.fn(),
}))
vi.mock('@/services/api/tasks', () => ({
  resumeTask: vi.fn(),
}))
vi.mock('@/services/websocket/GlobalWebSocket', () => ({
  globalWS: {
    subscribe: vi.fn(),
    unsubscribe: vi.fn(),
  },
}))

const mockGetTaskList = vi.mocked(getTaskList)
const mockResumeTask = vi.mocked(resumeTask)

function makeTask(overrides: Partial<TaskInfo> & { id: string }): TaskInfo {
  return {
    title: `任务 ${overrides.id}`,
    status: 'completed',
    created_at: '2026-09-01T10:00:00Z',
    ...overrides,
  } as TaskInfo
}

function renderPage() {
  return renderWithProviders(<DebugTasksPage />, { queryClient })
}

afterEach(() => {
  vi.clearAllMocks()
  queryClient.clear()
})

describe('DebugTasksPage 列表渲染', () => {
  it('七态 + 旧值折叠 + 未知值：状态文案按词表投影，行字段按列渲染', async () => {
    mockGetTaskList.mockResolvedValue({
      items: [
        makeTask({
          id: 't-done',
          status: 'completed',
          title: '完成件',
          agent_id: 'agent-a',
          current_phase: '收尾',
          message_count: 5,
          total_tokens: 120,
          ended: true,
        }),
        makeTask({ id: 't-run', status: 'running', title: '运行件', ended: false, message_count: 2 }),
        makeTask({ id: 't-eval', status: 'evaluating', title: '评估件' }),
        makeTask({ id: 't-fail', status: 'failed', title: '失败件' }),
        makeTask({ id: 't-timeout', status: 'timeout', title: '超时件' }),
        makeTask({ id: 't-pending', status: 'pending', title: '待执行件' }),
        makeTask({ id: 't-stop', status: 'stopped', title: '暂停件' }),
        makeTask({ id: 't-susp', status: 'suspended', title: '旧挂起件', task_id: 'task-9' }),
        makeTask({
          id: 't-unknown',
          status: 'planning',
          title: '未知件',
          created_at: 'not-a-date',
          agent_id: 'agent-b',
          description: '有描述无时间',
        }),
      ],
      total: 9,
    })
    renderPage()

    await screen.findByText('共 9 个任务')

    // 七态文案 + 旧值折叠 + 未知值，均按行内状态格断言（过滤按钮同文案不干扰）
    const rowOf = (title: string) => screen.getByText(title).closest('tr') as HTMLTableRowElement
    expect(within(rowOf('完成件')).getByText('已完成')).toBeInTheDocument()
    expect(within(rowOf('运行件')).getByText('运行中')).toBeInTheDocument()
    expect(within(rowOf('评估件')).getByText('评估中')).toBeInTheDocument()
    expect(within(rowOf('失败件')).getByText('已失败')).toBeInTheDocument()
    expect(within(rowOf('超时件')).getByText('已超时')).toBeInTheDocument()
    expect(within(rowOf('待执行件')).getByText('待执行')).toBeInTheDocument()
    expect(within(rowOf('暂停件')).getByText('已暂停')).toBeInTheDocument()
    // 旧值 suspended 折叠为「已暂停」；未知值按原串展示（不猜合法态）
    expect(within(rowOf('旧挂起件')).getByText('已暂停')).toBeInTheDocument()
    expect(within(rowOf('未知件')).getByText('planning')).toBeInTheDocument()

    // 派生字段：消息数 / token / 结束态
    expect(screen.getByText('5 消息')).toBeInTheDocument()
    expect(screen.getByText('120 tokens')).toBeInTheDocument()
    expect(screen.getByText('已结束')).toBeInTheDocument()
    expect(screen.getByText('进行中')).toBeInTheDocument()

    // 恢复按钮只在携带 task_id 的旧挂起值上出现
    expect(screen.getAllByRole('button', { name: '恢复' })).toHaveLength(1)

    // 时间无效 → 时间列显示 --（该行其余列均有值，-- 唯一来自时间格）
    const unknownRow = screen.getByText('未知件').closest('tr')
    expect(unknownRow).toHaveTextContent('--')

    // 时间有效的行不出现 --
    const doneRow = screen.getByText('完成件').closest('tr')
    expect(doneRow).not.toHaveTextContent('--')
  })

  it('标题/意图/名称全缺失时回退任务 ID，agent 缺失显示 --', async () => {
    mockGetTaskList.mockResolvedValue({
      items: [makeTask({ id: 'raw-id-42', title: undefined, agent_id: undefined })],
      total: 1,
    })
    renderPage()

    expect(await screen.findByText('raw-id-42')).toBeInTheDocument()
    const row = screen.getByText('raw-id-42').closest('tr')
    expect(row).toHaveTextContent('--')
  })
})

describe('DebugTasksPage 加载/错误/空态', () => {
  it('数据未返回时显示加载态，返回空列表后转空态', async () => {
    let resolveList!: (v: { items: TaskInfo[]; total: number }) => void
    mockGetTaskList.mockImplementation(
      () => new Promise((res) => { resolveList = res as (v: { items: TaskInfo[]; total: number }) => void }),
    )
    renderPage()

    expect(await screen.findByText('加载中...')).toBeInTheDocument()
    expect(screen.queryByText('暂无数据')).not.toBeInTheDocument()

    act(() => resolveList({ items: [], total: 0 }))
    expect(await screen.findByText('暂无数据')).toBeInTheDocument()
    expect(screen.queryByText('加载中...')).not.toBeInTheDocument()
    expect(screen.getByText('共 0 个任务')).toBeInTheDocument()
  })

  it('请求失败（Error）：错误横幅展示原始 message', async () => {
    mockGetTaskList.mockRejectedValue(new Error('后端超时'))
    renderPage()

    expect(await screen.findByText('后端超时')).toBeInTheDocument()
    expect(screen.queryByText('暂无数据')).not.toBeInTheDocument()
  })

  it('请求失败（非 Error 抛出值）：回退通用错误文案', async () => {
    mockGetTaskList.mockRejectedValue('boom')
    renderPage()

    expect(await screen.findByText('获取任务列表失败')).toBeInTheDocument()
  })
})

describe('DebugTasksPage 状态过滤', () => {
  it('点状态过滤按钮：以 (1, 20, 状态) 显式重拉；切回全部状态恢复 undefined', async () => {
    const user = userEvent.setup()
    mockGetTaskList.mockResolvedValue({ items: [], total: 0 })
    renderPage()
    await screen.findByText('暂无数据')
    expect(mockGetTaskList).toHaveBeenNthCalledWith(1, 1, 20, undefined)

    await user.click(screen.getByRole('button', { name: '已完成' }))
    await waitFor(() => {
      expect(mockGetTaskList).toHaveBeenLastCalledWith(1, 20, 'completed')
    })

    await user.click(screen.getByRole('button', { name: '全部状态' }))
    await waitFor(() => {
      expect(mockGetTaskList).toHaveBeenLastCalledWith(1, 20, undefined)
    })
  })
})

describe('DebugTasksPage WS 任务事件联动', () => {
  it('订阅两类任务事件，事件到达使列表重拉，卸载时退订', async () => {
    mockGetTaskList.mockResolvedValue({ items: [], total: 0 })
    const { unmount } = renderPage()
    await screen.findByText('暂无数据')

    const subscribeMock = vi.mocked(globalWS.subscribe)
    expect(subscribeMock).toHaveBeenCalledWith(WS_SERVER_EVENTS.TASK_STATUS_CHANGED, expect.any(Function))
    expect(subscribeMock).toHaveBeenCalledWith(WS_SERVER_EVENTS.TASK_STATUS_UPDATE, expect.any(Function))

    // 触发两类事件处理器 → debugTasks 缓存失效 → 组件重拉
    const handlers = subscribeMock.mock.calls.map((c) => c[1])
    const callsBefore = mockGetTaskList.mock.calls.length
    act(() => {
      handlers.forEach((h) => h())
    })
    await waitFor(() => {
      expect(mockGetTaskList.mock.calls.length).toBeGreaterThan(callsBefore)
    })

    unmount()
    const unsubscribeMock = vi.mocked(globalWS.unsubscribe)
    expect(unsubscribeMock).toHaveBeenCalledWith(WS_SERVER_EVENTS.TASK_STATUS_CHANGED, handlers[0])
    expect(unsubscribeMock).toHaveBeenCalledWith(WS_SERVER_EVENTS.TASK_STATUS_UPDATE, handlers[0])
  })
})

describe('DebugTasksPage 恢复任务', () => {
  it('恢复成功：调用 resumeTask(task_id)、按钮进入恢复中再复原，并重拉列表', async () => {
    const user = userEvent.setup()
    mockGetTaskList.mockResolvedValue({
      items: [makeTask({ id: 'row-9', status: 'suspended', title: '挂起任务', task_id: 'task-9' })],
      total: 1,
    })
    let resolveResume!: (v: TaskPauseResumeResponse) => void
    mockResumeTask.mockImplementation(
      () => new Promise((res) => { resolveResume = res }),
    )
    renderPage()
    await screen.findByText('挂起任务')

    await user.click(screen.getByRole('button', { name: '恢复' }))
    expect(mockResumeTask).toHaveBeenCalledWith('task-9')
    // 请求在途：按钮进入恢复中且禁用
    expect(screen.getByRole('button', { name: '恢复中...' })).toBeDisabled()

    act(() => resolveResume({ success: true, task_id: 'task-9', message: 'ok' }))
    await screen.findByRole('button', { name: '恢复' })
    expect(screen.getByRole('button', { name: '恢复' })).toBeEnabled()
    // 恢复成功后缓存失效，列表重拉
    await waitFor(() => {
      expect(mockGetTaskList.mock.calls.length).toBeGreaterThanOrEqual(2)
    })
  })

  it('恢复失败（Error）：错误横幅展示 message，错误期间列表隐藏', async () => {
    const user = userEvent.setup()
    mockGetTaskList.mockResolvedValue({
      items: [makeTask({ id: 'row-1', status: 'suspended', title: '挂起任务', task_id: 'task-1' })],
      total: 1,
    })
    mockResumeTask.mockRejectedValue(new Error('任务不存在'))
    renderPage()
    await screen.findByText('挂起任务')

    await user.click(screen.getByRole('button', { name: '恢复' }))
    expect(await screen.findByText('任务不存在')).toBeInTheDocument()
    // 操作错误期间列表让位给错误横幅
    expect(screen.queryByText('挂起任务')).not.toBeInTheDocument()
  })

  it('恢复失败（非 Error 抛出值）：回退通用失败文案', async () => {
    const user = userEvent.setup()
    mockGetTaskList.mockResolvedValue({
      items: [makeTask({ id: 'row-2', status: 'suspended', title: '挂起任务', task_id: 'task-2' })],
      total: 1,
    })
    mockResumeTask.mockRejectedValue('network-down')
    renderPage()
    await screen.findByText('挂起任务')

    await user.click(screen.getByRole('button', { name: '恢复' }))
    expect(await screen.findByText('恢复任务失败')).toBeInTheDocument()
  })
})

describe('DebugTasksPage 分页', () => {
  it('总页数 >1 时渲染分页：翻页以对应页码重拉，边界按钮禁用', async () => {
    const user = userEvent.setup()
    mockGetTaskList.mockResolvedValue({
      items: [makeTask({ id: 'only-row', title: '列表任务' })],
      total: 45,
    })
    renderPage()
    await screen.findByText('共 45 个任务')

    expect(screen.getByText('1 / 3')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '上一页' })).toBeDisabled()

    await user.click(screen.getByRole('button', { name: '下一页' }))
    expect(await screen.findByText('2 / 3')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '下一页' }))
    expect(await screen.findByText('3 / 3')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '下一页' })).toBeDisabled()

    await user.click(screen.getByRole('button', { name: '上一页' }))
    expect(await screen.findByText('2 / 3')).toBeInTheDocument()

    await waitFor(() => {
      expect(mockGetTaskList).toHaveBeenNthCalledWith(2, 2, 20, undefined)
    })
    expect(mockGetTaskList).toHaveBeenNthCalledWith(3, 3, 20, undefined)
    expect(mockGetTaskList).toHaveBeenNthCalledWith(4, 2, 20, undefined)
  })

  it('单页数据不渲染分页', async () => {
    mockGetTaskList.mockResolvedValue({
      items: [makeTask({ id: 'single', title: '唯一任务' })],
      total: 1,
    })
    renderPage()

    expect(await screen.findByText('共 1 个任务')).toBeInTheDocument()
    expect(screen.queryByText('上一页')).not.toBeInTheDocument()
    expect(screen.queryByText('下一页')).not.toBeInTheDocument()
  })
})
