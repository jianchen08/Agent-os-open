/** @feature FP-0.2.四 前端Schema(状态统一标记机制) | @ci: frontend-test */
/**
 * useMessageStateCard 纯逻辑车道——span 精确切除 + 声明路由（宿主零样式 id）。
 * mock 仅外部依赖（apiClient/registry/stores 由被测纯函数隔离），切片全真实。
 */
import { describe, expect, it } from 'vitest'
import { matchStateCardStyleId, stripBySpan } from '@/services/schema/messageStateCard'

describe('stripBySpan — span 精确切除', () => {
  it('标记在中间：前后正文都保留（切尾部丢后半正文是 bug）', () => {
    const text = '前段。<state>{"state": {}}</state>后段。'
    expect(stripBySpan(text, [3, 3 + '<state>{"state": {}}</state>'.length])).toBe(
      '前段。后段。',
    )
  })

  it('标记在尾部：只去标记', () => {
    const text = '正文。\n\n<state>{"state": {}}</state>'
    expect(stripBySpan(text, [4, text.length])).toBe('正文。')
  })

  it('非法区间原样返回（越界/倒序/空文本）', () => {
    expect(stripBySpan('abc', [5, 3])).toBe('abc')
    expect(stripBySpan('abc', [0, 99])).toBe('abc')
    expect(stripBySpan('', [0, 1])).toBe('')
  })
})

describe('matchStateCardStyleId — 声明路由（宿主零 id 硬编码）', () => {
  const cards = [
    { match: { marker: 'tool' }, style_id: 'tool_card' },
    { match: { marker: 'state' }, style_id: 'roleplay_state_card' },
  ]

  it('marker 命中 → style_id', () => {
    expect(matchStateCardStyleId(cards, 'state')).toBe('roleplay_state_card')
  })

  it('未命中/空声明集 → null（宿主零渲染语义）', () => {
    expect(matchStateCardStyleId(cards, 'mystery')).toBeNull()
    expect(matchStateCardStyleId([], 'state')).toBeNull()
  })

  it('style_id 形态非法 → 跳过该条继续', () => {
    expect(
      matchStateCardStyleId([{ match: { marker: 'state' }, style_id: 42 }], 'state'),
    ).toBeNull()
  })
})
