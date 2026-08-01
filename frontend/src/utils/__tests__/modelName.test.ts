/**
 * P8 模型显示名解析工具测试
 *
 * 覆盖 resolveModelDisplayName：
 * - 分级键（large/medium/small）映射为具体模型名
 * - 具体模型名原样返回
 * - 空值/未知键兜底
 */
import { describe, expect, it } from 'vitest'
import { resolveModelDisplayName } from '../modelName'

describe('resolveModelDisplayName', () => {
  const tiers = {
    large: 'deepseek-chat',
    medium: 'glm-4.5',
    small: 'minimax',
  }

  it('分级键 large 映射为具体模型名', () => {
    expect(resolveModelDisplayName('large', tiers)).toBe('deepseek-chat')
  })

  it('分级键 medium/small 映射为具体模型名', () => {
    expect(resolveModelDisplayName('medium', tiers)).toBe('glm-4.5')
    expect(resolveModelDisplayName('small', tiers)).toBe('minimax')
  })

  it('具体模型名原样返回', () => {
    expect(resolveModelDisplayName('deepseek-chat', tiers)).toBe('deepseek-chat')
    expect(resolveModelDisplayName('gpt-4o', tiers)).toBe('gpt-4o')
  })

  it('空值返回空串', () => {
    expect(resolveModelDisplayName('', tiers)).toBe('')
    expect(resolveModelDisplayName(undefined, tiers)).toBe('')
  })

  it('未知键且无 tiers 时原样返回', () => {
    expect(resolveModelDisplayName('large', undefined)).toBe('large')
    expect(resolveModelDisplayName('custom-model', undefined)).toBe('custom-model')
  })

  it('tiers 中键值为空时回退原值', () => {
    const emptyTier = { large: '' }
    expect(resolveModelDisplayName('large', emptyTier)).toBe('large')
  })
})
