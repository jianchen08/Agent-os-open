/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * 任务失败归因出口契约测试（P-21）
 *
 * 前端归因行的数据源 = 内核 /pipelines/state 摘要的 ``task.failure_class`` /
 * ``task.failure_reason`` 两键（插件域字段经 manifest export_fields 声明出口，
 * 内核零知识）。本测试把「插件写面」与「manifest 出口声明」对账：
 * 1. 写面证据：reconcile.py 的失败侧补写计划必须含两键（没写就没得出口）；
 * 2. 出口声明：tasks/plugin.json export_fields 必须声明两键（不声明 = 摘要
 *    静默过滤，前端永收不到——消费面死代码）。
 * 任一侧漂移即红，防「写了没出口 / 声明了没写」两类断链复发。
 */

import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

const REPO_ROOT = resolve(__dirname, '../../../..')
const MANIFEST_PATH = resolve(REPO_ROOT, 'plugins/shared/tasks/plugin.json')
const RECONCILE_PATH = resolve(REPO_ROOT, 'plugins/shared/tasks/reconcile.py')

describe('task.failure_* 出口契约（写面 ↔ manifest 出口声明）', () => {
  const manifest = JSON.parse(readFileSync(MANIFEST_PATH, 'utf-8')) as {
    export_fields?: string[]
  }
  const exported: string[] = manifest.export_fields ?? []
  const reconcileSource = readFileSync(RECONCILE_PATH, 'utf-8')

  it('失败侧写面含两键（reconcile.py failed 侧补随写）', () => {
    expect(reconcileSource).toContain('"task.failure_class"')
    expect(reconcileSource).toContain('"task.failure_reason"')
  })

  it('manifest export_fields 声明两键（不声明 = 摘要静默过滤，前端收不到）', () => {
    expect(exported).toContain('task.failure_class')
    expect(exported).toContain('task.failure_reason')
  })
})
