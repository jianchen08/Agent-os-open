// @feature FP-T12 前端组件补测
/**
 * DebugExecutionRecordsPage 局部 llm_usage 渲染回归测试
 *
 * 崩溃回归：llm_core 空 LLM 轮/流式中断路径写 `llm_usage = {}`（或仅带
 * model 归属键）——TraceRow 此前直呼 total_tokens.toLocaleString() 整页崩
 * （Cannot read properties of undefined）。判空后：缺数不渲染 token 段
 * 不猜 0，归属键仍展示。
 */
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import * as tracesApi from '@/services/api/pipelineDiagnostics'
import { renderWithProviders } from '@/test/renderWithProviders'
import { DebugExecutionRecordsPage } from '../DebugExecutionRecordsPage'

vi.mock('@/services/api/executionRecords', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/services/api/executionRecords')>()),
  getExecutionRecordsSessions: vi.fn(),
  getExecutionRecords: vi.fn(),
  clearAllExecutionRecords: vi.fn(),
}))
import { getExecutionRecordsSessions } from '@/services/api/executionRecords'
vi.mock('@/services/api/pipelineDiagnostics', () => ({
  getPipelineTraces: vi.fn(),
  getPipelineStateFull: vi.fn(),
}))

const mockTraces = vi.mocked(tracesApi.getPipelineTraces)

function trace(usage: Record<string, unknown> | null) {
  return {
    trace_id: 't-1',
    run_id: 'r-1',
    seq: 3,
    plugin_id: 'llm_core',
    patch_type: 'round_end',
    iteration: 1,
    tool_call_count: 0,
    llm_usage: usage,
    error: null,
    summary: '一轮',
    patch_data: {},
    created_at: '2026-09-10T00:00:00Z',
  }
}

beforeEach(() => {
  mockTraces.mockReset()
  vi.mocked(getExecutionRecordsSessions).mockResolvedValue({
    sessions: [
      { id: 'pipe-1', title: 'pipe-1', created_at: '2026-09-10T00:00:00Z', updated_at: '2026-09-10T00:00:00Z', record_count: null },
    ],
    total: 1,
  })
})

describe('局部 llm_usage 不崩（空对象/仅归属键）', () => {
  it('llm_usage={}（空 LLM 轮）渲染行，不猜 0 token', async () => {
    mockTraces.mockResolvedValue({ traces: [trace({})] })
    renderWithProviders(<DebugExecutionRecordsPage />)
    // 等会话选项装载（query 异步）再选管道
    await screen.findByText('pipe-1', { selector: 'option' })
    await userEvent.selectOptions(screen.getByRole('combobox'), 'pipe-1')
    expect(await screen.findByText(/#3/)).toBeInTheDocument()
    expect(screen.queryByText(/tok/)).not.toBeInTheDocument()
  })

  it('仅 model 归属键（无数字）渲染行 + 归属展示', async () => {
    mockTraces.mockResolvedValue({ traces: [trace({ model: 'glm-5.3' })] })
    renderWithProviders(<DebugExecutionRecordsPage />)
    // 等会话选项装载（query 异步）再选管道
    await screen.findByText('pipe-1', { selector: 'option' })
    await userEvent.selectOptions(screen.getByRole('combobox'), 'pipe-1')
    expect(await screen.findByText(/#3/)).toBeInTheDocument()
    expect(screen.getByText(/glm-5\.3/)).toBeInTheDocument()
    expect(screen.queryByText(/tok/)).not.toBeInTheDocument()
  })

  it('数字齐全路径不受影响（N tok · model）', async () => {
    mockTraces.mockResolvedValue({ traces: [trace({ total_tokens: 1234, model: 'glm-5.3' })] })
    renderWithProviders(<DebugExecutionRecordsPage />)
    // 等会话选项装载（query 异步）再选管道
    await screen.findByText('pipe-1', { selector: 'option' })
    await userEvent.selectOptions(screen.getByRole('combobox'), 'pipe-1')
    expect(await screen.findByText(/1,234 tok · glm-5\.3/)).toBeInTheDocument()
  })
})
