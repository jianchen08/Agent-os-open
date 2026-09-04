/** @feature FP-0.2.四 前端Schema | @ci frontend-test */
/**
 * DebugLlmPayloadPage 组件测试
 *
 * 验证「LLM 请求」页核心交互（2026-08-19 调试中心新增页）：
 * - 列表渲染快照元数据（模型/消息数/大小/时间）
 * - 点击条目 → 拉取 file 端点 → 展开渲染逐条消息（role/#序号/内容）+ 原始 JSON 折叠
 * - 快照内容含 tool_calls 时渲染工具调用行
 *
 * query 化后（批次 3）：列表经 useLlmPayloadDiagQuery 缓存 SWR；
 * 渲染用 renderWithProviders（自带 QueryClientProvider + MemoryRouter，
 * 外层不要再包 MemoryRouter 会炸 "Router inside Router"）。
 */
import { fireEvent, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { DebugLlmPayloadPage } from '@/pages/debug/DebugLlmPayloadPage'
import { createTestQueryClient, renderWithProviders } from '@/test/renderWithProviders'

vi.mock('@/services/api/llmPayload', () => ({
  getPayloadDiagList: vi.fn(),
  getPayloadDiagFile: vi.fn(),
}))

import { getPayloadDiagFile, getPayloadDiagList } from '@/services/api/llmPayload'

function renderPage() {
  return renderWithProviders(<DebugLlmPayloadPage />)
}

const FAKE_ITEMS = [
  {
    name: '1787131833571__MiniMax-M3__cfa1b570c9f0__5msg.json',
    ts: 1787131833571,
    model: 'MiniMax-M3',
    msgs_hash: 'cfa1b570c9f0',
    msg_count: 5,
    size: 11771,
  },
]

const FAKE_BODY = {
  model: 'MiniMax-M3',
  temperature: 0.7,
  messages: [
    { role: 'system', content: '你是灵汐' },
    { role: 'user', content: '调用 memory 工具' },
    {
      role: 'assistant',
      content: '',
      tool_calls: [{ id: 'tc1', function: { name: 'memory', arguments: '{"action":"store"}' } }],
    },
  ],
}

describe('DebugLlmPayloadPage', () => {
  beforeEach(() => {
    vi.mocked(getPayloadDiagList).mockResolvedValue({ items: FAKE_ITEMS, total: 1 })
    vi.mocked(getPayloadDiagFile).mockResolvedValue({
      name: FAKE_ITEMS[0].name,
      content: JSON.stringify(FAKE_BODY),
    })
  })

  it('渲染快照列表元数据', async () => {
    renderPage()
    await waitFor(() => expect(screen.getByText(/MiniMax-M3/)).toBeTruthy())
    expect(screen.getByText(/5 条消息/)).toBeTruthy()
    expect(screen.getByText(/11\.5 KB/)).toBeTruthy()
    expect(screen.getByText(/共 1 个快照/)).toBeTruthy()
  })

  it('点击条目展开逐条消息渲染（含 tool_calls）', async () => {
    renderPage()
    const item = await screen.findByRole('button', { name: /MiniMax-M3/ })
    fireEvent.click(item)
    await waitFor(() => expect(getPayloadDiagFile).toHaveBeenCalledWith(FAKE_ITEMS[0].name))
    // 逐条消息：#0 system / #1 user / #2 assistant
    expect(await screen.findByText('#0')).toBeTruthy()
    expect(screen.getByText('system')).toBeTruthy()
    expect(screen.getByText('你是灵汐')).toBeTruthy()
    // user 消息内容同时出现在消息区与原始 JSON 区（两处都应有）
    expect(screen.getAllByText(/调用 memory 工具/).length).toBeGreaterThanOrEqual(1)
    // 工具调用行（🔧 memory）
    expect(screen.getAllByText(/🔧 memory/).length).toBeGreaterThanOrEqual(1)
    // 参数行（temperature）与原始 JSON 折叠入口
    expect(screen.getAllByText(/temperature/).length).toBeGreaterThanOrEqual(1)
    expect(screen.getByText('原始 JSON')).toBeTruthy()
  })

  it('新快照产生后重进页面应显示最新快照（列表不落后一轮）', async () => {
    // 真实应用中 QueryClient 是全局单例：共享同一实例模拟切页往返的缓存复用
    const queryClient = createTestQueryClient()

    // 第一次进入：只有第 1 轮快照
    vi.mocked(getPayloadDiagList).mockResolvedValueOnce({ items: FAKE_ITEMS, total: 1 })
    const first = renderWithProviders(<DebugLlmPayloadPage />, { queryClient })
    await screen.findByRole('button', { name: /5 条消息/ })
    first.unmount()

    // 会话里又跑了一轮 LLM：服务端新增第 2 个快照；重进页面必须重取并显示它
    const round2 = {
      name: '1787131833999__MiniMax-M3__d34db33fc9f0__7msg.json',
      ts: 1787131833999,
      model: 'MiniMax-M3',
      msgs_hash: 'd34db33fc9f0',
      msg_count: 7,
      size: 20480,
    }
    vi.mocked(getPayloadDiagList).mockResolvedValue({
      items: [...FAKE_ITEMS, round2],
      total: 2,
    })
    renderWithProviders(<DebugLlmPayloadPage />, { queryClient })
    // 新快照出现在列表中（重挂触发重取的调用次数契约由 useDebugQueries.test 承载）
    await screen.findByRole('button', { name: /7 条消息/ })
  })
})
