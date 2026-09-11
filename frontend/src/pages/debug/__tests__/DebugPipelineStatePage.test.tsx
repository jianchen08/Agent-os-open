/** @feature FP-0.2.四 前端Schema | @ci frontend-test */
/**
 * DebugPipelineStatePage（管道 State 页）组件测试
 *
 * 验证：未选管道引导态且不发请求；选中管道后渲染 runs 表、state 摘要行与
 * 全字段表；字段名搜索过滤；JSON 值 pretty 展示。
 */
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import React from 'react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import * as api from '@/services/api/executionRecords'
import * as diag from '@/services/api/pipelineDiagnostics'
import { renderWithProviders } from '@/test/renderWithProviders'
import { DebugPipelineStatePage } from '../DebugPipelineStatePage'

// mock 整个 API 模块（vi.mock 提升到文件顶部，import 位置不影响生效）
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
const mockGetStateFull = vi.mocked(diag.getPipelineStateFull)

beforeEach(() => {
  vi.clearAllMocks()
  mockGetSessions.mockResolvedValue({ sessions: [], total: 0 })
})

const STATE_FULL = {
  pipeline_id: 'pipe-1',
  runs: [
    { run_id: 'run-a', status: 'completed', started_at: '2026-09-08T10:00:00Z', ended_at: '2026-09-08T10:01:00Z' },
  ],
  summary: { pipeline_id: 'pipe-1', message_count: 5 },
  fields: [
    { field_key: 'iteration', field_value: '7', updated_at: '2026-09-08T10:00:30Z' },
    { field_key: 'track.llm_usage', field_value: '{"total_tokens": 900}', updated_at: '2026-09-08T10:00:31Z' },
    { field_key: 'task.status', field_value: 'completed', updated_at: '2026-09-08T10:01:00Z' },
  ],
}

describe('DebugPipelineStatePage', () => {
  it('未选管道时显示引导态且不请求', async () => {
    renderWithProviders(<DebugPipelineStatePage />)
    await waitFor(() => {
      expect(screen.getByText(/选择一个管道/)).toBeInTheDocument()
    })
    expect(mockGetStateFull).not.toHaveBeenCalled()
  })

  it('选中管道后渲染 runs 表、摘要行与全字段表', async () => {
    const user = userEvent.setup()
    mockGetSessions.mockResolvedValue({
      sessions: [
        { id: 'pipe-1', title: '评测会话', created_at: '', updated_at: '', record_count: 5 },
      ],
      total: 1,
    })
    mockGetStateFull.mockResolvedValue(STATE_FULL)

    renderWithProviders(<DebugPipelineStatePage />)
    await waitFor(() => {
      expect(screen.getByRole('option', { name: /任务会话|评测会话/ })).toBeInTheDocument()
    })
    await user.selectOptions(screen.getByRole('combobox'), 'pipe-1')

    // 计数行 + runs 表 + 摘要 + 全字段（键与值）
    await waitFor(() => {
      expect(screen.getByText(/3 个字段 · 1 次 run/)).toBeInTheDocument()
    })
    expect(screen.getByText('run-a')).toBeInTheDocument()
    expect(screen.getByText('iteration')).toBeInTheDocument()
    expect(screen.getByText('task.status')).toBeInTheDocument()
    // JSON 值 pretty 展示（缩进后的键名）
    expect(screen.getByText(/"total_tokens": 900/)).toBeInTheDocument()
    // 摘要行 JSON
    expect(screen.getByText(/"message_count": 5/)).toBeInTheDocument()
  })

  it('字段名搜索过滤（子串匹配）', async () => {
    const user = userEvent.setup()
    mockGetSessions.mockResolvedValue({
      sessions: [
        { id: 'pipe-1', title: '评测会话', created_at: '', updated_at: '', record_count: 5 },
      ],
      total: 1,
    })
    mockGetStateFull.mockResolvedValue(STATE_FULL)

    renderWithProviders(<DebugPipelineStatePage />)
    await waitFor(() => {
      expect(screen.getByRole('option', { name: /任务会话|评测会话/ })).toBeInTheDocument()
    })
    await user.selectOptions(screen.getByRole('combobox'), 'pipe-1')
    await waitFor(() => {
      expect(screen.getByText('iteration')).toBeInTheDocument()
    })

    await user.type(screen.getByPlaceholderText(/按字段名过滤/), 'task')
    expect(screen.getByText('task.status')).toBeInTheDocument()
    expect(screen.queryByText('iteration')).not.toBeInTheDocument()
  })
})
