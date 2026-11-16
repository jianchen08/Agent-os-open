// @feature: FP-T12 前端适配（调试页执行记录懒加载补丁） | @ci: frontend-test
// @fix X27-F1 (P-153) TraceRow patch stringify 惰性化
/**
 * TraceRow patch stringify 惰性化回归测试
 *
 * X14 轮静态走查 P-153：TraceRow 每行无条件 JSON.stringify(patch_data)，
 * 200 行 × 大 patch 时折叠态也全量计算卡主线程。契约：折叠态零 stringify
 * （spy 断言），仅展开行计算；展开内容与修复前逐字符一致，交互零变化。
 */
import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { getExecutionRecordsSessions } from '@/services/api/executionRecords'
import * as tracesApi from '@/services/api/pipelineDiagnostics'
import { renderWithProviders } from '@/test/renderWithProviders'
import { DebugExecutionRecordsPage } from '../DebugExecutionRecordsPage'

// vi.mock 被 vitest 提升到文件顶，置于此处只为读序清晰
vi.mock('@/services/api/executionRecords', async (importOriginal) => ({
  ...(await importOriginal()),
  getExecutionRecordsSessions: vi.fn(),
  getExecutionRecords: vi.fn(),
  clearAllExecutionRecords: vi.fn(),
}))
vi.mock('@/services/api/pipelineDiagnostics', () => ({
  getPipelineTraces: vi.fn(),
  getPipelineStateFull: vi.fn(),
}))

const mockTraces = vi.mocked(tracesApi.getPipelineTraces)

/** 三条 trace 的 patch 形态互异（嵌套对象/数组对象/大字符串），防拟合 */
const patches: Record<string, unknown>[] = [
  { prompt: '你好', options: { temperature: 0.7, stop: ['\n'] } },
  { messages: [{ role: 'user', content: 'hi' }, { role: 'assistant', content: 'hello' }] },
  { blob: 'x'.repeat(5000) },
]
// 期望文本先于 spy 计算算了进来不算数：测试自身调用不得污染断言
const expectedTexts = patches.map((p) => JSON.stringify(p, null, 2))

function trace(seq: number, patchData: Record<string, unknown>) {
  return {
    trace_id: `t-${seq}`,
    pipeline_id: 'pipe-1',
    seq,
    plugin_id: 'llm_core',
    patch_type: 'round_end',
    created_at: '2026-09-10T00:00:00Z',
    iteration: 1,
    summary: `步 ${seq}`,
    llm_usage: null,
    error: null,
    tool_call_count: 0,
    patch_data: patchData,
  }
}

let stringifySpy: ReturnType<typeof vi.spyOn>

/** spy 捕获里落在三条 patch 上的调用数（测试自身的期望文本计算已前置排除） */
function patchCallCount(): number {
  return stringifySpy.mock.calls.filter(([arg]) => patches.some((p) => arg === p)).length
}

beforeEach(() => {
  stringifySpy = vi.spyOn(JSON, 'stringify')
  mockTraces.mockReset()
  mockTraces.mockResolvedValue({
    traces: patches.map((p, i) => trace(i + 1, p)),
    total: patches.length,
    pipeline_id: 'pipe-1',
  })
  vi.mocked(getExecutionRecordsSessions).mockResolvedValue({
    sessions: [
      {
        id: 'pipe-1',
        title: 'pipe-1',
        created_at: '2026-09-10T00:00:00Z',
        updated_at: '2026-09-10T00:00:00Z',
        record_count: null,
      },
    ],
    total: 1,
  })
})

afterEach(() => {
  stringifySpy.mockRestore()
})

async function renderWithTraces() {
  const view = renderWithProviders(<DebugExecutionRecordsPage />)
  await screen.findByText('pipe-1', { selector: 'option' })
  await userEvent.selectOptions(screen.getByRole('combobox'), 'pipe-1')
  await screen.findByText('#1')
  return view
}

describe('patch stringify 惰性化（X27-F1）', () => {
  it('折叠态渲染多行零 stringify，展开行内容逐字符一致', async () => {
    const { container } = await renderWithTraces()

    // 折叠态：patch 文本不计算（红线断言），不渲染任何 pre
    expect(patchCallCount()).toBe(0)
    expect(container.querySelectorAll('pre')).toHaveLength(0)

    // 展开第 1 行：patch 文本此刻才计算，内容与 JSON.stringify(..., null, 2) 逐字符一致
    const summaries = within(container).getAllByText('原始 patch')
    expect(summaries).toHaveLength(patches.length)
    await userEvent.click(summaries[0])
    await waitFor(() => expect(container.querySelectorAll('pre')).toHaveLength(1))
    const expandedPre = container.querySelector('pre')
    expect(expandedPre?.textContent).toBe(expectedTexts[0])
    expect(patchCallCount()).toBe(1)

    // 收起：pre 卸载，交互与修复前一致
    await userEvent.click(summaries[0])
    await waitFor(() => expect(container.querySelectorAll('pre')).toHaveLength(0))

    // 展开第 2 行（区分度输入 #2）：只算该行 patch，内容依旧逐字符一致
    const summariesAfter = within(container).getAllByText('原始 patch')
    await userEvent.click(summariesAfter[1])
    await waitFor(() => expect(container.querySelectorAll('pre')).toHaveLength(1))
    expect(container.querySelector('pre')?.textContent).toBe(expectedTexts[1])
    expect(patchCallCount()).toBe(2)
  })
})
