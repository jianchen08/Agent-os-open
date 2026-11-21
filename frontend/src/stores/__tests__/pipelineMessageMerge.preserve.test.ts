// @feature: FP-0.2.四 前端Schema | @ci: frontend-test
/** @ci: frontend-test */
/**
 * 全量对账降级守卫回归（2026-11-18 装机实测展开工具卡翻「已中断（无结果）」）。
 *
 * 原则：工具卡显示状态只能由后端消息权威证据决定；对账（渲染数据刷新）不得
 * 因取不到渲染数据而降级已有工具结果。
 */
import { describe, expect, it } from 'vitest'
import { preserveExistingToolResults } from '@/stores/pipelineMessageMerge'
import type { Message, MessagePart } from '@/types/models'

function assistant(id: string, parts: MessagePart[]): Message {
  return { id, sessionId: 's', role: 'assistant', content: '', timestamp: '', status: 'completed', parts } as Message
}

function toolPart(callId: string, overrides?: Record<string, unknown>): MessagePart {
  return { type: 'tool_call', callId, name: 'universal_search', args: {}, state: 'streaming', ...overrides } as MessagePart
}

describe('preserveExistingToolResults 全量对账降级守卫', () => {
  it('新 part 无结果而旧 part 有 → 保留旧 result 与 done 终态（不翻中断卡）', () => {
    const oldMsg = assistant('a1', [toolPart('call-1', { state: 'done', result: '预览内容' })])
    const apiMsg = assistant('a1', [toolPart('call-1')]) // 结果行被清理后的新映射
    const out = preserveExistingToolResults([apiMsg], [oldMsg])
    const part = out[0].parts![0] as any
    expect(part.result).toBe('预览内容')
    expect(part.state).toBe('done')
  })

  it('新 part 自带结果（补全/升级）→ 以新为准', () => {
    const oldMsg = assistant('a1', [toolPart('call-1', { state: 'done', result: '预览' })])
    const apiMsg = assistant('a1', [toolPart('call-1', { state: 'done', result: '全量内容' })])
    const out = preserveExistingToolResults([apiMsg], [oldMsg])
    expect((out[0].parts![0] as any).result).toBe('全量内容')
  })

  it('旧 part 也无结果 → 原样返回（真孤儿不伪造结果）', () => {
    const oldMsg = assistant('a1', [toolPart('call-1')])
    const apiMsg = assistant('a1', [toolPart('call-1')])
    const out = preserveExistingToolResults([apiMsg], [oldMsg])
    expect((out[0].parts![0] as any).result).toBeUndefined()
    expect(out[0].parts![0].state).toBe('streaming')
  })

  it('callId 不相交 → 原样返回', () => {
    const oldMsg = assistant('a1', [toolPart('call-old', { state: 'done', result: 'x' })])
    const apiMsg = assistant('a1', [toolPart('call-new')])
    const out = preserveExistingToolResults([apiMsg], [oldMsg])
    expect((out[0].parts![0] as any).result).toBeUndefined()
  })
})
