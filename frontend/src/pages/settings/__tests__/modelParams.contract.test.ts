// @feature FP-T12 前端组件补测
/** @ci: frontend-test */
/**
 * thinking 过滤口径对账契约测试（总纲 #13）
 *
 * 前端思考强度写入键（modelParams.STRENGTH_PARAM_KEYS）是 llm_core
 * _THINKING_STRENGTH_ALLOWED 白名单的镜像。本测试扫 llm_core 源码字面量
 * 对账：后端扩/删键时本测试变红（镜像漂移显式暴露，不再静默丢键）。
 * 档位词汇本体已由 llm_service /ext 预置端点声明下发，不在本闸范围。
 */

import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, it, expect } from 'vitest'
import { STRENGTH_PARAM_KEYS, emptyModelParamsDraft, buildModelFields } from '../modelParams'

const REPO_ROOT = resolve(__dirname, '../../../../..')
const LLM_CORE = 'plugins/shared/pipeline/core/llm_core/plugin.py'

/** 从 llm_core 源码解析 _THINKING_STRENGTH_ALLOWED 集合字面量 */
function parseBackendAllowedKeys(): string[] {
  const source = readFileSync(resolve(REPO_ROOT, LLM_CORE), 'utf-8')
  const marker = '_THINKING_STRENGTH_ALLOWED'
  const start = source.indexOf(marker)
  expect(start).toBeGreaterThan(-1, `llm_core 白名单 ${marker} 不存在——插件已重构，本闸需同步改版`)
  const open = source.indexOf('{', start)
  const close = source.indexOf('}', open)
  const block = source.slice(open + 1, close)
  const keys = [...block.matchAll(/"(\w+)"/g)].map((m) => m[1])
  expect(keys.length).toBeGreaterThan(0, '白名单集合解析为空')
  return keys
}

describe('thinking 过滤口径 ↔ llm_core 白名单对账', () => {
  it('前端写入键集合与 llm_core _THINKING_STRENGTH_ALLOWED 完全相等', () => {
    const backendKeys = parseBackendAllowedKeys()
    expect([...STRENGTH_PARAM_KEYS].sort()).toEqual([...backendKeys].sort())
  })

  it('buildModelFields 实际写出的键不越前端声明面（常量不腐化）', () => {
    const draft = emptyModelParamsDraft()
    draft.reasoningModel = true
    for (const level of Object.keys(draft.strength)) {
      draft.strength[level].thinkingType = 'enabled'
      draft.strength[level].effort = 'high'
    }
    const fields = buildModelFields(draft)
    const strength = fields.thinking_strength_params as Record<string, Record<string, unknown>>
    for (const entry of Object.values(strength)) {
      for (const key of Object.keys(entry)) {
        expect(STRENGTH_PARAM_KEYS).toContain(key)
      }
    }
  })
})
