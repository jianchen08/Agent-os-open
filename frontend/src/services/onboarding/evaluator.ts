/**
 * 完成条件求值器 — 纯函数（docs/decisions/2026-09-24-onboarding-plugin.md §5）。
 *
 * 设计：api_check 的取数与求值分离——组件先按 collectApiChecks() 清单拉取
 * 一次端点，把结果装进 EvaluationContext.apiResults，再调 evaluateCondition()
 * 纯求值；本模块不做任何 IO（可测性 + 服务端 content_schema.py 同词表）。
 */

import type { ApiCheckCondition, CompletionCondition } from './types'

/** 求值上下文：全部由组件预先装配（本模块零 IO） */
export interface EvaluationContext {
  /** endpoint → 已解析的 JSON 响应（未拉取的端点缺席 = 条件不满足） */
  apiResults: Map<string, unknown>
  /** 打开过的面板路径（/tasks 等） */
  visitedPanels: Set<string>
  /** 选过的模式键（task_mode） */
  selectedModes: Set<string>
  /** 点过 CTA 的步骤键（`${walkthroughId}/${stepId}`） */
  ctaClickedSteps: Set<string>
}

/** 展开复合条件，收集全部 api_check 叶子（供组件一次取数） */
export function collectApiChecks(cond: CompletionCondition): ApiCheckCondition[] {
  if (cond.type === 'all' || cond.type === 'any') {
    return cond.conditions.flatMap(collectApiChecks)
  }
  return cond.type === 'api_check' ? [cond] : []
}

/** 点路径取值（"defaults.chat" → data.defaults.chat）；任一层缺席返回 undefined */
export function readJsonPath(data: unknown, jsonPath: string | undefined): unknown {
  if (!jsonPath) return data
  let cur: unknown = data
  for (const key of jsonPath.split('.')) {
    if (typeof cur !== 'object' || cur === null || !(key in cur)) return undefined
    cur = (cur as Record<string, unknown>)[key]
  }
  return cur
}

/** non_empty 语义：数组有元素 / 对象有键 / 字符串非空；null/undefined 恒空 */
function isNonEmpty(value: unknown): boolean {
  if (value === null || value === undefined) return false
  if (Array.isArray(value)) return value.length > 0
  if (typeof value === 'object') return Object.keys(value).length > 0
  if (typeof value === 'string') return value.trim().length > 0
  return true
}

/** 纯求值：manual 恒 false（由持久化进度驱动），未取数端点不满足 */
export function evaluateCondition(
  cond: CompletionCondition,
  ctx: EvaluationContext,
  stepKey: string,
): boolean {
  switch (cond.type) {
    case 'all':
      return cond.conditions.every((c) => evaluateCondition(c, ctx, stepKey))
    case 'any':
      return cond.conditions.some((c) => evaluateCondition(c, ctx, stepKey))
    case 'api_check': {
      if (!ctx.apiResults.has(cond.endpoint)) return false
      return isNonEmpty(readJsonPath(ctx.apiResults.get(cond.endpoint), cond.json_path))
    }
    case 'panel_visited':
      return ctx.visitedPanels.has(cond.panel)
    case 'mode_selected':
      return ctx.selectedModes.has(cond.mode)
    case 'cta_clicked':
      return ctx.ctaClickedSteps.has(stepKey)
    default:
      return false
  }
}
