/** mergeStreamingParts 合并策略测试
 *
 * 权威语义（2026-08-22 修正）：后端下发的 parts 是落库权威完整形态
 * （new_message 的 data.message 经共享 mapper 还原，含全部轮次的
 * thinking/text/tool_call）。本地 parts 是流式增量累积，可能残缺，
 * 也可能携带 server 未下发的增量（tool_result 结果注入）。
 * 合并 = server 权威为基底 + 本地增量补充，绝不丢弃 server 权威文本。
 */
import { describe, it, expect } from 'vitest'
import { mergeStreamingParts } from '../utils'

describe('mergeStreamingParts', () => {
  it('server 权威完整形态（多轮 thinking/tool_call/text）为基底，本地残缺不覆盖', () => {
    // 本地累积：只有工具卡片（最终文本 chunk 丢失/乱序——bug 复现形态）
    const localParts = [
      { type: 'tool_call', callId: 'tc-1', name: 'search', args: {}, state: 'done', result: '结果1', sequence: 2 },
    ]
    // server：后端落库权威完整形态（mapper 还原，含全部轮次）
    const serverParts = [
      { type: 'thinking', content: '第一轮思考', state: 'done', sequence: 1 },
      { type: 'tool_call', callId: 'tc-1', name: 'search', args: {}, state: 'done', sequence: 2 },
      { type: 'thinking', content: '第二轮思考', state: 'done', sequence: 3 },
      { type: 'text', content: '最终回复', state: 'done', sequence: 4 },
    ]

    const { parts } = mergeStreamingParts(localParts, serverParts, '最终回复', '')

    // ★ 回归锚：server 的最终文本必须保留（此前本地有 tool_call 即整体丢弃 server）
    expect(parts.some((p: any) => p.type === 'text' && p.content === '最终回复')).toBe(true)
    // server 的 thinking 保留
    expect(parts.some((p: any) => p.type === 'thinking' && p.content === '第一轮思考')).toBe(true)
    expect(parts.some((p: any) => p.type === 'thinking' && p.content === '第二轮思考')).toBe(true)
    // server 的 tool_call 保留
    expect(parts.some((p: any) => p.type === 'tool_call' && p.callId === 'tc-1')).toBe(true)
  })

  it('本地 tool_result 结果注入 server 的 tool_call part（server 不带结果）', () => {
    const localParts = [
      { type: 'tool_call', callId: 'tc-1', name: 'search', args: {}, state: 'done', result: '搜索结果', sequence: 2 },
    ]
    const serverParts = [
      { type: 'tool_call', callId: 'tc-1', name: 'search', args: {}, state: 'done', sequence: 2 },
      { type: 'text', content: '基于结果的回答', state: 'done', sequence: 3 },
    ]

    const { parts } = mergeStreamingParts(localParts, serverParts, '基于结果的回答', '')

    const toolPart = parts.find((p: any) => p.type === 'tool_call' && p.callId === 'tc-1')
    expect(toolPart?.result).toBe('搜索结果')
    // server 文本保留
    expect(parts.some((p: any) => p.type === 'text' && p.content === '基于结果的回答')).toBe(true)
  })

  it('本地已累积但 server 未带的 text 追加保留（server 快照截断）', () => {
    const localParts = [
      { type: 'text', content: '本地累积正文', state: 'done', sequence: 1 },
    ]
    const serverParts = [
      { type: 'text', content: 'server 权威文本', state: 'done', sequence: 1 },
    ]

    const { parts } = mergeStreamingParts(localParts, serverParts, 'server 权威文本', '本地累积正文')

    const texts = parts.filter((p: any) => p.type === 'text').map((p: any) => p.content)
    expect(texts).toContain('server 权威文本')
    expect(texts).toContain('本地累积正文')
  })

  it('本地为空时，用 serverParts 兜底', () => {
    const serverParts = [
      { type: 'thinking', content: '后端思考', state: 'done', sequence: 1 },
      { type: 'text', content: '后端文本', state: 'done', sequence: 2 },
    ]
    const { parts } = mergeStreamingParts([], serverParts, '后端文本', '')
    expect(parts).toBe(serverParts)
  })

  it('本地只有空内容 part 时，用 serverParts 兜底', () => {
    // 流式占位符残留：parts 存在但全部无实质内容
    const localParts = [
      { type: 'text', content: '', state: 'streaming', sequence: 1 },
    ]
    const serverParts = [
      { type: 'text', content: '后端文本', state: 'done', sequence: 1 },
    ]
    const { parts } = mergeStreamingParts(localParts, serverParts, '后端文本', '')
    expect(parts).toBe(serverParts)
  })

  it('server full_content 更长时校准 content', () => {
    const localParts = [{ type: 'text', content: '部分', state: 'done', sequence: 1 }]
    const { content, parts } = mergeStreamingParts(
      localParts, [{ type: 'text', content: 'x', state: 'done', sequence: 1 }],
      '完整的最终文本内容', '部分',
    )
    // parts 以 server 为基底（server 有 text part）
    expect(parts.some((p: any) => p.type === 'text' && p.content === 'x')).toBe(true)
    expect(content).toBe('完整的最终文本内容')
  })

  it('本地 content 更长时保留本地 content', () => {
    const localParts = [{ type: 'text', content: '本地更长的完整内容', state: 'done', sequence: 1 }]
    const { content } = mergeStreamingParts(
      localParts, [{ type: 'text', content: 'x', state: 'done', sequence: 1 }],
      '短', '本地更长的完整内容',
    )
    expect(content).toBe('本地更长的完整内容')
  })

  it('本地和 server 都为空时返回空', () => {
    const { parts, content } = mergeStreamingParts([], [], undefined, '')
    expect(parts).toEqual([])
    expect(content).toBe('')
  })

  it('server 无 parts（旧后端）时本地原样保留并收敛 streaming 态', () => {
    const localParts = [
      { type: 'tool_call', callId: 'tc-1', name: 'search', args: {}, state: 'done', sequence: 1 },
      { type: 'text', content: '本地文本', state: 'streaming', sequence: 2 },
    ]
    const { parts } = mergeStreamingParts(localParts, undefined, undefined, '本地文本')
    expect(parts.some((p: any) => p.type === 'tool_call' && p.callId === 'tc-1')).toBe(true)
    const textPart = parts.find((p: any) => p.type === 'text')
    expect(textPart?.content).toBe('本地文本')
    expect(textPart?.state).toBe('done')
  })

  it('本地残留 streaming 态在 server 基底上收敛为 done', () => {
    const localParts = [
      { type: 'text', content: '本地文本', state: 'streaming', sequence: 1 },
    ]
    const serverParts = [
      { type: 'text', content: 'server 文本', state: 'done', sequence: 1 },
    ]
    const { parts } = mergeStreamingParts(localParts, serverParts, 'server 文本', '本地文本')
    expect(parts.every((p: any) => p.state !== 'streaming')).toBe(true)
  })
})
