// @feature FP-0.2.四 前端Schema onboarding 完成条件评估 | @ci frontend-test
import { describe, expect, it } from 'vitest'
import { collectApiChecks, evaluateCondition, readJsonPath } from './evaluator'
import type { EvaluationContext } from './evaluator'
import type { CompletionCondition } from './types'

function ctx(overrides: Partial<EvaluationContext> = {}): EvaluationContext {
  return {
    apiResults: new Map(),
    visitedPanels: new Set(),
    selectedModes: new Set(),
    ctaClickedSteps: new Set(),
    ...overrides,
  }
}

describe('readJsonPath', () => {
  const data = { providers: { deepseek: {} }, defaults: { chat: 'x' }, list: [1, 2] }

  it('点路径逐层下探（≥2 组有区分度输入）', () => {
    expect(readJsonPath(data, 'defaults.chat')).toBe('x')
    expect(readJsonPath(data, 'providers')).toEqual({ deepseek: {} })
    expect(readJsonPath(data, 'list.0')).toBe(1) // 数组下标经 in 命中，同键语义
  })

  it('任一层缺席返回 undefined；缺省路径返回根', () => {
    expect(readJsonPath(data, 'a.b.c')).toBeUndefined()
    expect(readJsonPath(data, undefined)).toBe(data)
  })
})

describe('evaluateCondition: api_check', () => {
  const cond: CompletionCondition = {
    type: 'api_check',
    endpoint: '/ext/llm_service/config/llm',
    json_path: 'providers',
    op: 'non_empty',
  }

  it('未取数端点恒不满足（不猜）', () => {
    expect(evaluateCondition(cond, ctx(), 'k')).toBe(false)
  })

  it('non_empty 语义分型：数组/对象/字符串/空值', () => {
    const mk = (v: unknown) => ctx({ apiResults: new Map([['/ext/llm_service/config/llm', v]]) })
    expect(evaluateCondition(cond, mk({ providers: { deepseek: {} } }), 'k')).toBe(true)
    expect(evaluateCondition(
      { ...cond, json_path: 'defaults.chat' },
      mk({ defaults: { chat: 'gpt' } }),
      'k',
    )).toBe(true)
    expect(evaluateCondition(cond, mk({ providers: {} }), 'k')).toBe(false)
    expect(evaluateCondition(cond, mk({ defaults: { chat: '' } }), 'k')).toBe(false)
    // 数值/布尔等标量：非空即真（isNonEmpty 兜底分支）
    expect(evaluateCondition(cond, mk({ providers: { count: 42 } }), 'k')).toBe(true)
  })

  it('性质：空串/空数组/空对象/缺键 全部视为空（一致口径）', () => {
    const mk = (v: unknown) => ctx({ apiResults: new Map([['/api/v1/n', v]]) })
    for (const v of [{ threads: [] }, { threads: undefined }, null, {}]) {
      expect(evaluateCondition(
        { type: 'api_check', endpoint: '/api/v1/n', json_path: 'threads', op: 'non_empty' },
        mk(v),
        'k',
      )).toBe(false)
    }
  })
})

describe('evaluateCondition: 事件型条件', () => {
  it('panel_visited 命中/未命中', () => {
    const cond: CompletionCondition = { type: 'panel_visited', panel: '/tasks' }
    expect(evaluateCondition(cond, ctx({ visitedPanels: new Set(['/tasks']) }), 'k')).toBe(true)
    expect(evaluateCondition(cond, ctx(), 'k')).toBe(false)
  })

  it('mode_selected 命中/未命中', () => {
    const cond: CompletionCondition = { type: 'mode_selected', mode: 'mode_roleplay' }
    expect(evaluateCondition(cond, ctx({ selectedModes: new Set(['mode_roleplay']) }), 'k')).toBe(true)
    expect(evaluateCondition(cond, ctx(), 'k')).toBe(false)
  })

  it('cta_clicked 以步骤键判定', () => {
    const cond: CompletionCondition = { type: 'cta_clicked' }
    expect(evaluateCondition(cond, ctx({ ctaClickedSteps: new Set(['w/s']) }), 'w/s')).toBe(true)
    expect(evaluateCondition(cond, ctx({ ctaClickedSteps: new Set(['w/s']) }), 'w/other')).toBe(false)
  })

  it('manual 恒 false（由持久化进度驱动，非求值器职责）', () => {
    expect(evaluateCondition({ type: 'manual' }, ctx({ visitedPanels: new Set(['x']) }), 'k')).toBe(false)
  })
})

describe('evaluateCondition: 复合条件', () => {
  const ok: CompletionCondition = { type: 'panel_visited', panel: '/tasks' }
  const bad: CompletionCondition = { type: 'panel_visited', panel: '/nowhere' }

  it('all 要求全部满足；any 任一满足', () => {
    const c = ctx({ visitedPanels: new Set(['/tasks']) })
    expect(evaluateCondition({ type: 'all', conditions: [ok, ok] }, c, 'k')).toBe(true)
    expect(evaluateCondition({ type: 'all', conditions: [ok, bad] }, c, 'k')).toBe(false)
    expect(evaluateCondition({ type: 'any', conditions: [ok, bad] }, c, 'k')).toBe(true)
    expect(evaluateCondition({ type: 'any', conditions: [bad, bad] }, c, 'k')).toBe(false)
  })

  it('嵌套复合（all 内嵌 any）', () => {
    const nested: CompletionCondition = {
      type: 'all',
      conditions: [ok, { type: 'any', conditions: [bad, ok] }],
    }
    expect(evaluateCondition(nested, ctx({ visitedPanels: new Set(['/tasks']) }), 'k')).toBe(true)
  })
})

describe('collectApiChecks', () => {
  it('展开复合条件收集全部 api_check 叶子；非 api_check 返回空', () => {
    const a: CompletionCondition = { type: 'api_check', endpoint: '/api/v1/n', op: 'non_empty' }
    const b: CompletionCondition = {
      type: 'api_check',
      endpoint: '/ext/llm_service/config/llm',
      json_path: 'providers',
      op: 'non_empty',
    }
    expect(collectApiChecks({ type: 'all', conditions: [a, { type: 'any', conditions: [b, a] }] })).toEqual([a, b, a])
    expect(collectApiChecks({ type: 'panel_visited', panel: '/tasks' })).toEqual([])
    expect(collectApiChecks({ type: 'manual' })).toEqual([])
  })
})
