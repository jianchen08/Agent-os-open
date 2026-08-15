/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * 功能测试：思考强度 ← 管道模型参数反向映射
 *
 * 推演链：需求「新会话/管道标签的思考强度直接拿对应管道的参数映射」→ 决策：
 * 用户未显式设置强度时，从该管道模型的 default_params 反向推断强度档位：
 * - reasoning_effort（low/medium/high/max）→ 直接映射（max 视为 high）
 * - thinking.type === 'disabled' → off
 * - 无 effort 字段时按 temperature 推断（>=0.7 → low，<=0.4 → high）
 * - 无法判断 → null（调用方回退默认档）
 * 用户显式设置过的标签（localStorage 有值）始终优先，不被管道参数覆盖。
 */

import { describe, expect, it } from 'vitest'
import { mapParamsToStrength, findModelParams } from '@/utils/thinkingStrength'

describe('mapParamsToStrength — 管道参数反向映射强度', () => {
  it('reasoning_effort 直接映射（low/medium/high）', () => {
    expect(mapParamsToStrength({ reasoning_effort: 'low' })).toBe('low')
    expect(mapParamsToStrength({ reasoning_effort: 'medium' })).toBe('medium')
    expect(mapParamsToStrength({ reasoning_effort: 'high' })).toBe('high')
  })

  it('reasoning_effort=max 视为 high', () => {
    expect(mapParamsToStrength({ reasoning_effort: 'max' })).toBe('high')
  })

  it('thinking.type=disabled → off（显式关闭思考）', () => {
    expect(mapParamsToStrength({ thinking: { type: 'disabled' } })).toBe('off')
    // enabled/adaptive 不映射为 off
    expect(mapParamsToStrength({ thinking: { type: 'enabled' } })).not.toBe('off')
    expect(mapParamsToStrength({ thinking: { type: 'adaptive' } })).not.toBe('off')
  })

  it('无 effort 时按 temperature 推断：>=0.7 → low，<=0.4 → high，中间 → null', () => {
    expect(mapParamsToStrength({ temperature: 0.7 })).toBe('low')
    expect(mapParamsToStrength({ temperature: 0.9 })).toBe('low')
    expect(mapParamsToStrength({ temperature: 0.3 })).toBe('high')
    expect(mapParamsToStrength({ temperature: 0.5 })).toBeNull()
  })

  it('无法判断（空/仅 max_tokens）→ null', () => {
    expect(mapParamsToStrength(undefined)).toBeNull()
    expect(mapParamsToStrength({})).toBeNull()
    expect(mapParamsToStrength({ max_tokens: 4096 })).toBeNull()
  })

  it('effort 优先于 temperature（不一致时以 effort 为准）', () => {
    expect(mapParamsToStrength({ reasoning_effort: 'high', temperature: 0.9 })).toBe('high')
  })
})

describe('findModelParams — 从 LLM 配置定位模型参数', () => {
  const models = {
    'deepseek-v4-pro-apigo': {
      provider: 'openai',
      model_name: 'deepseek-v4-pro',
      display_name: 'DeepSeek V4 Pro',
      default_params: { temperature: 0.3, reasoning_effort: 'high' },
    },
    'minimax-m3': {
      provider: 'minimax',
      model_name: 'MiniMax-M3',
      display_name: 'MiniMax M3',
      default_params: { temperature: 0.7 },
    },
  }

  it('按 model_name 匹配', () => {
    expect(findModelParams(models, 'deepseek-v4-pro')).toEqual({
      temperature: 0.3,
      reasoning_effort: 'high',
    })
  })

  it('按 key（model_id）匹配兜底', () => {
    expect(findModelParams(models, 'minimax-m3')).toEqual({ temperature: 0.7 })
  })

  it('未命中 → undefined', () => {
    expect(findModelParams(models, 'no-such-model')).toBeUndefined()
    expect(findModelParams(undefined, 'x')).toBeUndefined()
  })
})
