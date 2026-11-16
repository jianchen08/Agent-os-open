/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * 任务失败归因展示映射测试（P-21）
 *
 * 锁 failureClassLabel 的三面契约：
 * 1. 词表内两词 → 「人话（原词）」；
 * 2. 词表外 → 原词透传（后端新增分类词不猜语义）；
 * 3. 空值 → 空串（字段缺失回退现状，调用方按空不渲染）。
 */

import { describe, expect, it } from 'vitest'
import { FAILURE_CLASS_LABELS, failureClassLabel } from '../failureAttribution'

describe('failureClassLabel', () => {
  it('词表内：run_stopped/run_aborted → 人话（原词）', () => {
    expect(failureClassLabel('run_stopped')).toBe('运行终止（run_stopped）')
    expect(failureClassLabel('run_aborted')).toBe('原因未记录（run_aborted）')
  })

  it('词表外原词透传（不猜语义）', () => {
    expect(failureClassLabel('future_class')).toBe('future_class')
  })

  it('空值回退空串（字段缺失 = 现状，不渲染）', () => {
    expect(failureClassLabel('')).toBe('')
  })

  it('词表与后端 reconcile 失败分类词对账（两词收录，无多余键）', () => {
    // 后端真值 = plugins/shared/tasks/reconcile.py _FAILURE_CLASS_*；键集合
    // 漂移（漏收/多收）即红——多收的词没有后端写面证据，不得进展示词表。
    expect(Object.keys(FAILURE_CLASS_LABELS).sort()).toEqual(['run_aborted', 'run_stopped'])
  })
})
