// @feature: FP-0.2.四 前端Schema | @ci: frontend-test
/**
 * DebugLlmPayloadPage 覆盖补测：消息内容多形态解析 + 快照读取失败面
 *
 * 既有测试（DebugLlmPayloadPage.test.tsx）覆盖列表元数据与 string content 的
 * 展开链路；本文件补「真实 LLM 请求体」的多形态解析与读取失败面：
 * - content 为分段数组：字符串分段 / {type:'text',text} 分段 / 不可解析项被丢弃
 * - content 非 string 非数组 → 不渲染内容块（不产出脏文本）
 * - 超长文本截断提示（2000 字符边界两侧各一组输入）
 * - tool_calls 缺 id / 缺 function.name 的兜底标识
 * - reasoning_content 折叠块（有/无两组输入）
 * - 快照读取业务错误（error 字段）与坏 JSON（解析失败提示）
 * - 快照读取传输错误：Error 取 message，非 Error 回退通用文案
 *
 * 测试策略：mock 外部边界（llmPayload API 网络层），组件真实渲染。
 * 查询助手说明：详情区同时有「逐条消息」与「原始 JSON」两个 <pre>，正文可能两处
 * 命中——用精确 textContent 匹配（等值语义）区分内容块与原始 JSON 文本。
 */
import { fireEvent, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { DebugLlmPayloadPage } from '@/pages/debug/DebugLlmPayloadPage'
import { getPayloadDiagFile, getPayloadDiagList } from '@/services/api/llmPayload'
import { renderWithProviders } from '@/test/renderWithProviders'
import type { PayloadDiagItem } from '@/services/api/llmPayload'

vi.mock('@/services/api/llmPayload', () => ({
  getPayloadDiagList: vi.fn(),
  getPayloadDiagFile: vi.fn(),
}))

const ITEM: PayloadDiagItem = {
  name: '1787131833571__MiniMax-M3__cfa1b570c9f0__5msg.json',
  ts: 1787131833571,
  model: 'MiniMax-M3',
  msgs_hash: 'cfa1b570c9f0',
  msg_count: 5,
  size: 11771,
}

/** 打开唯一快照的详情（列表就绪后点击条目） */
async function openDetail(): Promise<void> {
  renderWithProviders(<DebugLlmPayloadPage />)
  const item = await screen.findByRole('button', { name: /MiniMax-M3/ })
  fireEvent.click(item)
  await waitFor(() => expect(getPayloadDiagFile).toHaveBeenCalledWith(ITEM.name))
}

/** 以指定请求体内容供给 file 端点 */
function mockBody(body: Record<string, unknown> | string) {
  vi.mocked(getPayloadDiagFile).mockResolvedValue({
    name: ITEM.name,
    content: typeof body === 'string' ? body : JSON.stringify(body),
  })
}

/** 按精确文本等值定位内容块（区别于同屏共存的原始 JSON 文本） */
function contentBlock(expected: string): HTMLElement {
  return screen.getByText((_t, el) => el?.tagName === 'PRE' && el.textContent === expected)
}

/** 按元素类型 + 精确文案定位（应对 JSX 插值把文案拆成多个文本节点的场景） */
function byTagExact(tag: string, exact: string): HTMLElement {
  return screen.getByText((_t, el) => el?.tagName === tag && el.textContent === exact)
}

beforeEach(() => {
  vi.resetAllMocks()
  vi.mocked(getPayloadDiagList).mockResolvedValue({ items: [ITEM], total: 1 })
})

describe('DebugLlmPayloadPage — 消息内容多形态', () => {
  it('分段数组中的字符串分段按行拼接展示', async () => {
    mockBody({
      model: 'MiniMax-M3',
      messages: [{ role: 'user', content: ['第一段', '第二段'] }],
    })
    await openDetail()

    // 两段落拼进同一内容块（换行分隔），原始 JSON 区是另一形态的文本节点
    expect(await waitFor(() => contentBlock('第一段\n第二段'))).toBeInTheDocument()
  })

  it('分段数组中的 {type,text} 对象分段取 text 字段（视觉块形态）', async () => {
    mockBody({
      model: 'MiniMax-M3',
      messages: [
        {
          role: 'user',
          content: [
            { type: 'text', text: '看图里的报错' },
            { type: 'image_url', image_url: { url: 'http://x/a.png' } },
          ],
        },
      ],
    })
    await openDetail()

    // 无 text 字段的分段贡献空串并被丢弃
    expect(await waitFor(() => contentBlock('看图里的报错'))).toBeInTheDocument()
  })

  it('不可解析分段（数字/布尔/null/无 text 对象）被丢弃，不产出脏文本', async () => {
    mockBody({
      model: 'MiniMax-M3',
      messages: [{ role: 'user', content: [42, true, null, { no_text: 1 }, '保留下来的文本'] }],
    })
    await openDetail()

    expect(await waitFor(() => contentBlock('保留下来的文本'))).toBeInTheDocument()
    expect(screen.queryByText(/undefined/)).not.toBeInTheDocument()
    expect(screen.queryByText(/\[object Object\]/)).not.toBeInTheDocument()
  })

  it('content 非字符串非数组（如对象）→ 不渲染内容块，角色与 name 仍展示', async () => {
    mockBody({
      model: 'MiniMax-M3',
      messages: [{ role: 'assistant', content: { weird: true }, name: '工具调用者' }],
    })
    await openDetail()

    expect(await screen.findByText('#0')).toBeInTheDocument()
    expect(screen.getByText('assistant')).toBeInTheDocument()
    expect(screen.getByText('工具调用者')).toBeInTheDocument()
    // 唯一 pre 是原始 JSON 区：无内容块被渲染
    expect(document.querySelectorAll('pre')).toHaveLength(1)
    expect(screen.queryByText(/\[object Object\]/)).not.toBeInTheDocument()
  })

  it('role 缺失 → 占位 ?（不让消息块无角色标识）', async () => {
    mockBody({
      model: 'MiniMax-M3',
      messages: [{ content: '无角色消息' }],
    })
    await openDetail()

    expect(await screen.findByText('?')).toBeInTheDocument()
    expect(contentBlock('无角色消息')).toBeInTheDocument()
  })
})

describe('DebugLlmPayloadPage — 长文本截断', () => {
  it('恰好 2000 字符 → 全量展示，无截断提示', async () => {
    const exactly2000 = 'B'.repeat(2000)
    mockBody({ model: 'MiniMax-M3', messages: [{ role: 'user', content: exactly2000 }] })
    await openDetail()

    expect(await waitFor(() => contentBlock(exactly2000))).toBeInTheDocument()
    expect(screen.queryByText(/展开原始 JSON 看全文/)).not.toBeInTheDocument()
  })

  it('超过 2000 字符一位 → 截断到 2000 并提示总字符数', async () => {
    const tooLong = 'A'.repeat(2001)
    mockBody({ model: 'MiniMax-M3', messages: [{ role: 'user', content: tooLong }] })
    await openDetail()

    const block = await waitFor(() =>
      screen.getByText(
        (_t, el) =>
          el?.tagName === 'PRE' &&
          el.textContent === `${'A'.repeat(2000)}\n…（共 2001 字符，展开原始 JSON 看全文）`,
      ),
    )
    expect(block).toBeInTheDocument()
  })

  it('远超上限（10000 字符）→ 截断长度恒为 2000（不随原文长度变化）', async () => {
    const huge = 'C'.repeat(10000)
    mockBody({ model: 'MiniMax-M3', messages: [{ role: 'user', content: huge }] })
    await openDetail()

    const block = await waitFor(() =>
      screen.getByText(
        (_t, el) =>
          el?.tagName === 'PRE' &&
          (el.textContent ?? '').includes('共 10000 字符'),
      ),
    )
    expect(block.textContent).toBe(`${'C'.repeat(2000)}\n…（共 10000 字符，展开原始 JSON 看全文）`)
  })
})

describe('DebugLlmPayloadPage — 工具调用与思考过程', () => {
  it('tool_calls 缺 id / 缺 function.name → 序号兜底标识，参数照常展示', async () => {
    mockBody({
      model: 'MiniMax-M3',
      messages: [
        {
          role: 'assistant',
          content: '',
          tool_calls: [{ function: { name: 'memory', arguments: '{"a":1}' } }, { id: 'tc2' }],
        },
      ],
    })
    await openDetail()

    expect(await screen.findByText(/🔧 memory/)).toBeInTheDocument()
    expect(screen.getByText(/🔧 call-1/)).toBeInTheDocument()
    expect(screen.getByText(/\{"a":1\}/)).toBeInTheDocument()
  })

  it('无 tool_calls → 不渲染工具调用区块（仅内容块）', async () => {
    mockBody({
      model: 'MiniMax-M3',
      messages: [{ role: 'assistant', content: '纯文本回复' }],
    })
    await openDetail()

    expect(await waitFor(() => contentBlock('纯文本回复'))).toBeInTheDocument()
    expect(screen.queryByText(/🔧/)).not.toBeInTheDocument()
  })

  it('reasoning_content 存在 → 渲染思考过程折叠块（标注字符数与原文长度一致）', async () => {
    const reasoning = '让我想想……因为这里有缓存命中问题'
    mockBody({
      model: 'MiniMax-M3',
      messages: [{ role: 'assistant', content: '结论', reasoning_content: reasoning }],
    })
    await openDetail()

    // 字符数标注取自原文长度（性质断言：标签数值 = 实际长度，不写死字面值）
    const summary = await waitFor(() =>
      byTagExact('SUMMARY', `思考过程（${reasoning.length} 字符）`),
    )
    expect(summary).toBeInTheDocument()
    expect(summary.nextElementSibling?.textContent).toBe(reasoning)
  })

  it('无 reasoning_content → 不渲染思考过程块', async () => {
    mockBody({
      model: 'MiniMax-M3',
      messages: [{ role: 'assistant', content: '结论' }],
    })
    await openDetail()

    expect(await waitFor(() => contentBlock('结论'))).toBeInTheDocument()
    expect(screen.queryByText(/思考过程/)).not.toBeInTheDocument()
  })

  it('model 与附加参数由快照内容驱动逐项展示（不沿用列表元数据）', async () => {
    mockBody({
      model: 'DeepSeek-V4',
      temperature: 0.3,
      messages: [{ role: 'user', content: 'hi' }],
    })
    await openDetail()

    expect(await screen.findByText('DeepSeek-V4')).toBeInTheDocument()
    expect(screen.getByText('temperature: 0.3')).toBeInTheDocument()
    // 参数区不得重复展示 messages / model（已由专用位置渲染）
    expect(screen.queryByText(/^messages:/)).not.toBeInTheDocument()
  })

  it('请求体缺 model → 详情徽章回退列表条目的 model（两处 model 文案并存）', async () => {
    mockBody({ messages: [{ role: 'user', content: 'hi' }] })
    await openDetail()

    await waitFor(() => expect(contentBlock('hi')).toBeInTheDocument())
    // 列表条目 + 详情徽章各一处（回退失效只剩列表一处空徽章）
    expect(screen.getAllByText('MiniMax-M3')).toHaveLength(2)
  })
})

describe('DebugLlmPayloadPage — 快照读取失败面', () => {
  it('业务错误信封（error 字段）→ 展示该错误文案', async () => {
    vi.mocked(getPayloadDiagFile).mockResolvedValue({
      name: ITEM.name,
      content: '',
      error: '快照文件已被清理',
    })
    await openDetail()

    expect(await screen.findByText('快照文件已被清理')).toBeInTheDocument()
    expect(screen.queryByText('原始 JSON')).not.toBeInTheDocument()
  })

  it('坏 JSON → 提示解析失败，不渲染原始 JSON 折叠区', async () => {
    mockBody('{ not valid json')
    await openDetail()

    expect(await screen.findByText('快照 JSON 解析失败')).toBeInTheDocument()
    expect(screen.queryByText('原始 JSON')).not.toBeInTheDocument()
  })

  it('传输失败（Error）→ 展示错误 message', async () => {
    vi.mocked(getPayloadDiagFile).mockRejectedValue(new Error('connection reset'))
    await openDetail()

    expect(await screen.findByText('connection reset')).toBeInTheDocument()
  })

  it('传输失败（非 Error）→ 回退通用文案', async () => {
    vi.mocked(getPayloadDiagFile).mockRejectedValue({ status: 500 })
    await openDetail()

    expect(await screen.findByText('读取快照失败')).toBeInTheDocument()
  })

  it('请求体解析为 null（裸 "null" 快照）→ 详情区不渲染任何内容且不报错', async () => {
    // JSON.parse('null') 合法但非对象：不得当成可渲染 body（否则崩溃或展示脏文本）
    mockBody('null')
    await openDetail()

    await waitFor(() => expect(screen.queryByText('读取快照失败')).not.toBeInTheDocument())
    // 详情区整体不渲染（既无原始 JSON 折叠区，也无消息块）
    expect(screen.queryByText('原始 JSON')).not.toBeInTheDocument()
    expect(screen.queryByText('#0')).not.toBeInTheDocument()
    // 列表条目本身仍在（失败被局限在详情区内）
    expect(screen.getByRole('button', { name: /MiniMax-M3/ })).toBeInTheDocument()
  })

  it('请求体解析为标量字符串 → 无消息块，但原始 JSON 仍可查看（不静默吞掉快照）', async () => {
    mockBody('"just a string"')
    await openDetail()

    expect(await screen.findByText('原始 JSON')).toBeInTheDocument()
    expect(screen.queryByText('#0')).not.toBeInTheDocument()
    // model 徽章回退列表条目（标量无 model 字段）
    expect(screen.getAllByText('MiniMax-M3').length).toBeGreaterThanOrEqual(1)
  })

  it('请求体解析为 false → 不渲染详情（falsy 快照落空分支）', async () => {
    mockBody('false')
    await openDetail()

    await waitFor(() => expect(screen.queryByText('读取快照失败')).not.toBeInTheDocument())
    expect(screen.queryByText('原始 JSON')).not.toBeInTheDocument()
    expect(screen.queryByText('#0')).not.toBeInTheDocument()
  })

  it('再次点击同一条目收起详情（详情与列表切换语义）', async () => {
    mockBody({
      model: 'MiniMax-M3',
      messages: [{ role: 'user', content: '唯一内容' }],
    })
    renderWithProviders(<DebugLlmPayloadPage />)
    const item = await screen.findByRole('button', { name: /MiniMax-M3/ })

    fireEvent.click(item)
    expect(await waitFor(() => contentBlock('唯一内容'))).toBeInTheDocument()

    fireEvent.click(item)
    expect(screen.queryByText('唯一内容')).not.toBeInTheDocument()
  })
})
