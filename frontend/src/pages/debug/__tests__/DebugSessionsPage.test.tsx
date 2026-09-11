/**
 * DebugSessionsPage / DebugExecutionRecordsPage 组件测试
 *
 * 验证调试中心两个页面能正确消费 executionRecords API 返回的数据并渲染表格。
 * 背景：后端 /execution/records* 端点从 stub 接到真实 storage 后，
 * 需确认前端组件的数据获取→状态更新→表格渲染链路完整。
 */
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import React from 'react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import * as api from '@/services/api/executionRecords'
import * as diag from '@/services/api/pipelineDiagnostics'
import { renderWithProviders } from '@/test/renderWithProviders'
import { DebugExecutionRecordsPage } from '../DebugExecutionRecordsPage'
import { DebugSessionsPage } from '../DebugSessionsPage'

// mock 整个 API 模块（vi.mock 提升到文件顶部， import 位置不影响生效）
vi.mock('@/services/api/executionRecords', () => ({
  getExecutionRecordsSessions: vi.fn(),
  getExecutionRecords: vi.fn(),
  clearAllExecutionRecords: vi.fn(),
}))
vi.mock('@/services/api/pipelineDiagnostics', () => ({
  getPipelineTraces: vi.fn(),
  getPipelineStateFull: vi.fn(),
}))

const mockGetSessions = vi.mocked(api.getExecutionRecordsSessions)
const mockGetTraces = vi.mocked(diag.getPipelineTraces)

beforeEach(() => {
  vi.clearAllMocks()
})

describe('DebugSessionsPage', () => {
  it('加载成功后渲染会话表格', async () => {
    mockGetSessions.mockResolvedValue({
      sessions: [
        {
          id: '504f14e3d403',
          title: '测试会话标题',
          created_at: '2026-07-01T10:00:00',
          updated_at: '2026-07-01T10:00:00',
          record_count: 42,
        },
      ],
      total: 1,
    })

    renderWithProviders(<DebugSessionsPage />)

    // 等待数据渲染
    await waitFor(() => {
      expect(screen.getByText('共 1 个会话')).toBeInTheDocument()
    })

    // 会话 ID 出现在表格中（移动端卡片 + 桌面端表格两处渲染）
    expect(screen.getAllByText('504f14e3d403').length).toBeGreaterThan(0)
    // 标题渲染
    expect(screen.getAllByText('测试会话标题').length).toBeGreaterThan(0)
    // 记录数渲染
    expect(screen.getAllByText('42').length).toBeGreaterThan(0)
    // 不再显示加载态
    expect(screen.queryByText('加载中...')).not.toBeInTheDocument()
    // 不显示空状态
    expect(screen.queryByText('暂无数据')).not.toBeInTheDocument()
  })

  it('加载失败时显示错误提示', async () => {
    mockGetSessions.mockRejectedValue(new Error('网络错误'))

    renderWithProviders(<DebugSessionsPage />)

    await waitFor(() => {
      expect(screen.getByText('网络错误')).toBeInTheDocument()
    })
  })

  it('无数据时显示空状态', async () => {
    mockGetSessions.mockResolvedValue({ sessions: [], total: 0 })

    renderWithProviders(<DebugSessionsPage />)

    await waitFor(() => {
      expect(screen.getByText('暂无数据')).toBeInTheDocument()
    })
  })
})

describe('DebugExecutionRecordsPage（执行 Trace 视图）', () => {
  it('未选管道时显示引导态且不请求 trace', async () => {
    mockGetSessions.mockResolvedValue({
      sessions: [
        {
          id: 'pipe-1',
          title: '任务会话',
          created_at: '2026-09-08T10:00:00',
          updated_at: '2026-09-08T10:00:00',
          record_count: 3,
        },
      ],
      total: 1,
    })

    renderWithProviders(<DebugExecutionRecordsPage />)

    await waitFor(() => {
      expect(screen.getByText(/选择一个管道/)).toBeInTheDocument()
    })
    expect(mockGetTraces).not.toHaveBeenCalled()
  })

  it('选中管道后加载 trace 并按插件/轮次/错误渲染', async () => {
    const user = userEvent.setup()
    mockGetSessions.mockResolvedValue({
      sessions: [
        {
          id: 'pipe-1',
          title: '任务会话',
          created_at: '2026-09-08T10:00:00',
          updated_at: '2026-09-08T10:00:00',
          record_count: 3,
        },
      ],
      total: 1,
    })
    mockGetTraces.mockResolvedValue({
      pipeline_id: 'pipe-1',
      total: 3,
      traces: [
        {
          trace_id: 't1', run_id: 'r1', seq: 1, plugin_id: 'context_build',
          patch_type: 'state_update', created_at: '2026-09-08T10:00:01Z',
          iteration: null, summary: '加载 agent 配置', llm_usage: null,
          error: null, tool_call_count: 0, patch_data: {},
        },
        {
          trace_id: 't2', run_id: 'r1', seq: 2, plugin_id: 'llm_core',
          patch_type: 'state_update', created_at: '2026-09-08T10:00:02Z',
          iteration: 1, summary: '回复内容', llm_usage: {
            input_tokens: 100, output_tokens: 20, total_tokens: 120,
            cached_tokens: 0, model: 'MiniMax-M3',
          },
          error: null, tool_call_count: 1, patch_data: { iteration: 1 },
        },
        {
          trace_id: 't3', run_id: 'r1', seq: 3, plugin_id: 'llm_core',
          patch_type: 'error', created_at: '2026-09-08T10:00:03Z',
          iteration: 2, summary: null,
          llm_usage: null, error: 'CAPABILITY_TIMEOUT after 5000ms',
          tool_call_count: 0, patch_data: { raw_error: 'CAPABILITY_TIMEOUT after 5000ms' },
        },
      ],
    })

    renderWithProviders(<DebugExecutionRecordsPage />)

    // 选中管道（下拉切换 = 换缓存条目，触发 trace 请求）
    await waitFor(() => {
      expect(screen.getByRole('option', { name: /任务会话|评测会话/ })).toBeInTheDocument()
    })
    await user.selectOptions(screen.getByRole('combobox'), 'pipe-1')

    // 步数统计 + 插件徽章 + 轮次分组 + 错误呈现
    await waitFor(() => {
      expect(screen.getByText('共 3 步')).toBeInTheDocument()
    })
    expect(screen.getAllByText('llm_core').length).toBeGreaterThan(0)
    // 错误文本在错误块与展开 patch 的 JSON 里各出现一次
    expect(screen.getAllByText(/CAPABILITY_TIMEOUT/).length).toBeGreaterThan(0)
    // 「轮 1」在分组标题与行内徽章各出现一次
    expect(screen.getAllByText('轮 1').length).toBeGreaterThan(0)
    expect(screen.getByText(/前置 \/ 后置步/)).toBeInTheDocument()
  })
})
