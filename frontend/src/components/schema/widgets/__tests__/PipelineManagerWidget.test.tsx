/**
 * PipelineManagerWidget 组件测试（2026-08-19 调试中心批次）
 *
 * 验证任务管理面板（管道总览）三个行为修复：
 * - 未知状态的任务不再被丢弃（原 taskStatusToPipelineStatus 返回 null 即 continue）；
 * - 条目行显示任务原始状态（evaluating 等细态不被 4 态映射吞掉）；
 * - 展开详情含 state 真值行（任务状态/State 状态/已结束/当前阶段/消息条数）。
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi, beforeEach } from 'vitest'

vi.mock('@/services/api/client', () => ({
  default: { get: vi.fn() },
}))
vi.mock('@/services/api/tasks', () => ({
  pauseTask: vi.fn(),
  resumeTask: vi.fn(),
  cancelTask: vi.fn(),
}))
vi.mock('@/services/pipelineNavigator', () => ({
  navigateToPipeline: vi.fn(),
}))

const FAKE_RUNS: Record<string, unknown> = {}
const FAKE_STATES: Record<string, unknown> = {
  pipeA: {
    pipeline_id: 'pipeA',
    thread_id: 'th-a',
    source: 'memory',
    state: {
      current_phase: 'exit',
      ended: true,
      status: 'active',
      'task.status': 'completed',
      message_count: 12,
      raw_error: null,
    },
  },
}

vi.mock('@/stores/pipelineRegistryStore', () => ({
  usePipelineRegistryStore: Object.assign(
    (sel: (s: unknown) => unknown) => sel({ runs: FAKE_RUNS, states: FAKE_STATES }),
    {
      getState: () => ({
        runs: FAKE_RUNS,
        fetch: vi.fn(),
        startAutoRefresh: vi.fn(),
        stopAutoRefresh: vi.fn(),
      }),
    },
  ),
}))
vi.mock('@/stores/contextUsageStore', () => ({
  useContextUsageStore: (sel: (s: unknown) => unknown) => sel({ usageByPipeline: {} }),
}))
vi.mock('@/stores/sessionStore', () => ({
  useSessionStore: (sel: (s: unknown) => unknown) => sel({ sessions: [] }),
}))

import { PipelineManagerWidget } from '@/components/schema/widgets/PipelineManagerWidget'
import apiClient from '@/services/api/client'

/** 任务列表（channel_api tasks）：一个常规状态 + 一个未知状态 */
const FAKE_TASKS = {
  items: [
    {
      id: 'task-eval',
      title: '评估中任务',
      status: 'evaluating', // 4 态映射 → running
      pipeline_run_id: 'pipeA',
      agent_name: 'general_agent',
    },
    {
      id: 'task-weird',
      title: '未知状态任务',
      status: 'pending_review', // 4 态映射 → null（原实现直接丢弃）
      pipeline_run_id: 'pipeB',
      agent_name: 'general_agent',
    },
  ],
}

describe('PipelineManagerWidget', () => {
  beforeEach(() => {
    vi.mocked(apiClient.get).mockResolvedValue({ data: FAKE_TASKS })
  })

  it('未知状态任务保留且显示原始状态', async () => {
    render(<PipelineManagerWidget />)
    // 两个任务都出现（未知状态不再被吞）；树视图+列表视图双渲染 → 用 getAll
    expect((await screen.findAllByText('评估中任务')).length).toBeGreaterThanOrEqual(1)
    expect((await screen.findAllByText('未知状态任务')).length).toBeGreaterThanOrEqual(1)
    // 原始状态文本可见（细态不被 4 态文案吞掉）
    expect((await screen.findAllByText('evaluating')).length).toBeGreaterThanOrEqual(1)
    expect((await screen.findAllByText('pending_review')).length).toBeGreaterThanOrEqual(1)
  })

  it('展开详情含 state 真值行', async () => {
    render(<PipelineManagerWidget />)
    // 展开任务节点（TaskRow 点击=toggle），再点其下条目行首的 chevron（行主体点击是"打开对话"）
    fireEvent.click((await screen.findAllByText('评估中任务'))[0])
    const afterNode = await screen.findAllByText('评估中任务')
    for (const el of afterNode.slice(1)) {
      const row = el.closest('div')
      const chevron = row?.querySelector('button')
      if (chevron) fireEvent.click(chevron)
    }
    await waitFor(() => expect(screen.getAllByText('State 状态').length).toBeGreaterThanOrEqual(1))
    expect(screen.getAllByText('已结束').length).toBeGreaterThanOrEqual(1)
    expect(screen.getAllByText('当前阶段').length).toBeGreaterThanOrEqual(1)
    expect(screen.getAllByText('消息条数').length).toBeGreaterThanOrEqual(1)
    // state['task.status'] = completed 的真值出现在详情中
    expect(screen.getAllByText('completed').length).toBeGreaterThanOrEqual(1)
  })
})
