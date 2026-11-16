/** @feature FP-T12 前端适配（thinking part 判空） @vision V6 可即用 @ci frontend-test */
/**
 * filterBlankMessages 判空依据测试（thinking 单份化配套）：
 * 思考只存 parts（顶层 thinking 已停写），空白 assistant 判定必须走
 * thinking part 的内容，纯函数不依赖 store/网络。
 */
import { describe, it, expect } from 'vitest'
import { filterBlankMessages } from '../pipelineMessageMerge'
import type { Message } from '@/types/models'

const mk = (overrides: Partial<Message>): Message =>
  ({
    id: 'a1',
    sessionId: 's1',
    sequence: 1,
    role: 'assistant',
    content: '',
    timestamp: '2026-10-09T00:00:00Z',
    status: 'completed',
    ...overrides,
  }) as Message

describe('filterBlankMessages：思考判空走 thinking part', () => {
  it('仅含非空 thinking part（无 content）→ 保留（纯思考消息不误删）', () => {
    const kept = filterBlankMessages([
      mk({ parts: [{ type: 'thinking', content: '只有思考', state: 'done' }] }),
    ])
    expect(kept).toHaveLength(1)
  })

  it('空白 thinking part：消息经 hasParts 保留（既有行为，与思考判定无关）', () => {
    const kept = filterBlankMessages([
      mk({ parts: [{ type: 'thinking', content: '   ', state: 'done' }] }),
    ])
    expect(kept).toHaveLength(1)
  })

  it('遗留形态：仅顶层 thinking（无 parts，停写前旧数据）→ 过滤（不再作为判空依据）', () => {
    const kept = filterBlankMessages([
      mk({ thinking: { content: '旧顶层思考', isThinking: false } }),
    ])
    expect(kept).toHaveLength(0)
  })

  it('完全空白（无 content/parts/thinking，非 streaming）→ 过滤', () => {
    const kept = filterBlankMessages([mk({})])
    expect(kept).toHaveLength(0)
  })

  it('对照组：有正文 → 保留；streaming 占位无内容 → 保留', () => {
    const kept = filterBlankMessages([
      mk({ content: '有正文' }),
      mk({ id: 'a2', status: 'streaming' }),
    ])
    expect(kept).toHaveLength(2)
  })
})
