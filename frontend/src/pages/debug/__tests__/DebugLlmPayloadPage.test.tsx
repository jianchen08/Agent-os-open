/**
 * DebugLlmPayloadPage 组件测试
 *
 * 验证「LLM 请求」页核心交互（2026-08-19 调试中心新增页）：
 * - 列表渲染快照元数据（模型/消息数/大小/时间）
 * - 点击条目 → 拉取 file 端点 → 展开渲染逐条消息（role/#序号/内容）+ 原始 JSON 折叠
 * - 快照内容含 tool_calls 时渲染工具调用行
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { MemoryRouter } from 'react-router-dom'
import { DebugLlmPayloadPage } from '@/pages/debug/DebugLlmPayloadPage'

vi.mock('@/services/api/llmPayload', () => ({
  getPayloadDiagList: vi.fn(),
  getPayloadDiagFile: vi.fn(),
}))

import { getPayloadDiagFile, getPayloadDiagList } from '@/services/api/llmPayload'

function renderPage() {
  return render(
    <MemoryRouter>
      <DebugLlmPayloadPage />
    </MemoryRouter>,
  )
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
})
