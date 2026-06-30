/** mergeStreamingParts 合并策略测试 */
import { describe, it, expect } from 'vitest'
import { mergeStreamingParts } from '../utils'

describe('mergeStreamingParts', () => {
  it('本地有完整多轮内容时，优先保留本地（不被末轮残缺 serverParts 覆盖）', () => {
    // 本地累积：2轮思考+工具+最终文本（完整真相）
    const localParts = [
      { type: 'thinking', content: '第一轮思考', state: 'done', sequence: 1 },
      { type: 'tool_call', callId: 'tc-1', name: 'search', args: {}, state: 'done', result: '结果1', sequence: 2 },
      { type: 'thinking', content: '第二轮思考', state: 'done', sequence: 3 },
      { type: 'tool_call', callId: 'tc-2', name: 'read', args: {}, state: 'done', result: '结果2', sequence: 4 },
      { type: 'text', content: '最终回复', state: 'done', sequence: 5 },
    ]
    // serverParts：末轮残缺，且数量 >= 本地（旧逻辑会触发覆盖）
    const serverParts = [
      { type: 'thinking', content: '只有最后一轮', state: 'done', sequence: 1 },
      { type: 'text', content: '残缺', state: 'done', sequence: 2 },
      { type: 'text', content: '残缺', state: 'done', sequence: 3 },
      { type: 'text', content: '残缺', state: 'done', sequence: 4 },
      { type: 'text', content: '残缺', state: 'done', sequence: 5 },
      { type: 'text', content: '残缺', state: 'done', sequence: 6 },
    ]

    const { parts } = mergeStreamingParts(localParts, serverParts, '最终回复', '最终回复')

    // 本地完整内容应被保留
    expect(parts.some((p: any) => p.type === 'thinking' && p.content === '第一轮思考')).toBe(true)
    expect(parts.some((p: any) => p.type === 'tool_call' && p.callId === 'tc-1')).toBe(true)
    expect(parts.some((p: any) => p.type === 'tool_call' && p.callId === 'tc-2')).toBe(true)
    // serverParts 不应混入
    expect(parts.some((p: any) => p.content === '残缺')).toBe(false)
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
    // parts 保留本地（有实质内容），content 用更长的 server 版本校准
    expect(parts).toBe(localParts)
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

  it('本地有 tool_call 但无 text 时仍视为有内容（保留本地）', () => {
    const localParts = [
      { type: 'tool_call', callId: 'tc-1', name: 'search', args: {}, state: 'done', sequence: 1 },
    ]
    const serverParts = [
      { type: 'text', content: '后端文本', state: 'done', sequence: 1 },
    ]
    const { parts } = mergeStreamingParts(localParts, serverParts, '后端文本', '')
    expect(parts).toBe(localParts)
  })
})
