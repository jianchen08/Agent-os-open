/**
 * messageCardBody — message_cards 本体形态纯逻辑测试
 * （ADR 2026-10-14-message-card-body-form）
 *
 * 覆盖：
 * - matchBubbleCardStyleId：kind=bubble 命中 / modes 会话域门 / kind 缺省 =
 *   追加形态不消费 / 多声明声明序首个命中 / 形态非法跳过
 * - buildBubbleCardPayload：载荷信封形状（agent_id/presenter 键缺席式）
 */
import { describe, expect, it } from 'vitest'
import { buildBubbleCardPayload, matchBubbleCardStyleId } from '../messageCardBody'

function card(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return { match: { kind: 'bubble' }, style_id: 'bubble_card', ...overrides }
}

describe('matchBubbleCardStyleId — 本体形态声明路由', () => {
  it('kind=bubble 命中（modes 缺省 = 会话域不限，mode null 也生效）', () => {
    expect(matchBubbleCardStyleId([card()], null)).toBe('bubble_card')
    expect(matchBubbleCardStyleId([card()], 'roleplay')).toBe('bubble_card')
  })

  it('modes 门：活跃会话 mode 在域内生效，域外/null 不生效（两组有区分度）', () => {
    const decl = [card({ match: { kind: 'bubble', modes: ['roleplay'] } })]
    expect(decl && matchBubbleCardStyleId(decl, 'roleplay')).toBe('bubble_card')
    expect(matchBubbleCardStyleId(decl, 'coding')).toBeNull()
    expect(matchBubbleCardStyleId(decl, null)).toBeNull()
  })

  it('modes 空数组 = 不限域（与缺省同语义）', () => {
    const decl = [card({ match: { kind: 'bubble', modes: [] } })]
    expect(matchBubbleCardStyleId(decl, null)).toBe('bubble_card')
  })

  it('kind 缺省（marker 追加形态声明）不消费——追加/本体双形态并存零互扰', () => {
    const markerForm = [{ match: { marker: 'state' }, style_id: 'state_card' }]
    expect(matchBubbleCardStyleId(markerForm, 'roleplay')).toBeNull()
    expect(matchBubbleCardStyleId(markerForm, null)).toBeNull()
  })

  it('多声明按声明序首个命中；kind=bubble 但 style_id 非法跳过', () => {
    const decl = [
      card({ style_id: 42 }),
      card({ style_id: '', match: { kind: 'bubble', modes: ['roleplay'] } }),
      card({ style_id: 'second_hit', match: { kind: 'bubble', modes: ['roleplay'] } }),
    ]
    expect(matchBubbleCardStyleId(decl, 'roleplay')).toBe('second_hit')
  })

  it('空声明表 / 非 bubble 混合表 → null（回退宿主默认渲染）', () => {
    expect(matchBubbleCardStyleId([], 'roleplay')).toBeNull()
    expect(matchBubbleCardStyleId([{ other: 1 }], 'roleplay')).toBeNull()
  })
})

describe('buildBubbleCardPayload — 本体形态载荷信封', () => {
  it('role/status 透传；agent_id/presenter 键缺席式（缺席不占键）', () => {
    expect(buildBubbleCardPayload('user', 'completed', undefined, null)).toEqual({
      kind: 'bubble',
      role: 'user',
      status: 'completed',
      presenter: null,
    })
    expect(buildBubbleCardPayload('assistant', undefined, undefined, null)).toEqual({
      kind: 'bubble',
      role: 'assistant',
      status: '',
      presenter: null,
    })
  })

  it('agent_id / presenter 在场完整携带（presenter emoji 串与色对双形态）', () => {
    const withAgent = buildBubbleCardPayload('assistant', 'completed', 'mode_roleplay/card_a', {
      name: '林晚',
      avatar: '🌙',
    })
    expect(withAgent.agent_id).toBe('mode_roleplay/card_a')
    expect(withAgent.presenter).toEqual({ name: '林晚', avatar: '🌙' })

    const withPair = buildBubbleCardPayload('assistant', 'completed', undefined, {
      name: 'Aki',
      avatar: { fg: '#fff', bg: '#333' },
    })
    expect(withPair.presenter).toEqual({ name: 'Aki', avatar: { fg: '#fff', bg: '#333' } })
  })
})
