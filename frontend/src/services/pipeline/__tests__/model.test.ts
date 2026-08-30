/**
 * services/pipeline/model 单元测试
 *
 * 覆盖 0.2 模型的纯函数辅助：
 * - 格式判断 / id 收集
 * - 引用四类分类（plugin/step/template/unknown）
 * - raw data 路径不可变更新（set/delete/insert/move + 未知字段保真）
 *
 * 类型口径 = G10 文件 DSL：转移在 `next:`（then 目标字符串）、体级循环为
 * `while`；旧内部形态（routes/exit_routes/体级 loop_config）不得出现。
 */

import { describe, it, expect } from 'vitest'
import {
  collectBodyIds,
  collectStepIds,
  deleteAtPath,
  getLoopBodies,
  insertAtPath,
  isPipelineV2Data,
  isTemplateRef,
  moveArrayItem,
  resolveRef,
  setAtPath,
} from '../model'

const sample = {
  name: 'autonomous',
  custom_top_level: 'keep-me', // 模型外字段：保存不丢
  loop_bodies: [
    { id: 'init', steps: [{ id: 'init', steps: ['pipeline_a'] }] },
    {
      id: 'main',
      while: 'True',
      steps: [
        { id: 'prepare', steps: ['pipeline_b', '{{state.core_plugin}}'] },
        { id: 'post', steps: ['pipeline_c'], next: [{ when: 'True', then: 'end' }] },
      ],
    },
    { id: 'exit', run_on_error: true, steps: [] },
  ],
}

describe('model — 格式判断与 id 收集', () => {
  it('isPipelineV2Data：有非空 loop_bodies 才是 0.2', () => {
    expect(isPipelineV2Data(sample)).toBe(true)
    expect(isPipelineV2Data({ name: 'x', input_routes: [] })).toBe(false)
    expect(isPipelineV2Data({ loop_bodies: [] })).toBe(false)
    expect(isPipelineV2Data(null)).toBe(false)
  })

  it('getLoopBodies / collectStepIds / collectBodyIds', () => {
    expect(getLoopBodies(sample).map((b) => b.id)).toEqual(['init', 'main', 'exit'])
    expect(collectStepIds(sample)).toEqual(['init', 'prepare', 'post'])
    expect(collectBodyIds(sample)).toEqual(['init', 'main', 'exit'])
    // 非 0.2 数据安全返回空
    expect(collectStepIds({})).toEqual([])
  })
})

describe('model — 引用分类', () => {
  const catalog = [{ id: 'pipeline_a' }, { id: 'pipeline_b' }]
  const stepIds = new Set(['init', 'prepare', 'post'])

  it('四类命中：plugin / step / template / unknown', () => {
    expect(resolveRef('pipeline_a', catalog, stepIds)).toEqual({
      kind: 'plugin',
      catalogEntry: { id: 'pipeline_a' },
    })
    expect(resolveRef('prepare', catalog, stepIds).kind).toBe('step')
    expect(resolveRef('{{state.core_plugin}}', catalog, stepIds).kind).toBe('template')
    expect(resolveRef('doc_extract', catalog, stepIds).kind).toBe('unknown')
  })

  it('isTemplateRef', () => {
    expect(isTemplateRef('{{state.x}}')).toBe(true)
    expect(isTemplateRef('plugin_a')).toBe(false)
  })
})

describe('model — raw data 路径不可变更新', () => {
  it('setAtPath：写入嵌套路径并创建缺失中间对象，原对象不被修改', () => {
    const next = setAtPath(sample, ['loop_bodies', 2, 'while'], 'True')
    expect(next.loop_bodies[2].while).toBe('True')
    expect(sample.loop_bodies[2].while).toBeUndefined()
  })

  it('deleteAtPath：删除对象 key 与数组元素', () => {
    const noNext = deleteAtPath(sample, ['loop_bodies', 1, 'steps', 1, 'next'])
    expect(noNext.loop_bodies[1].steps[1].next).toBeUndefined()
    const spliced = deleteAtPath(sample, ['loop_bodies', 1, 'steps', 1])
    expect(spliced.loop_bodies[1].steps.map((s) => s.id)).toEqual(['prepare'])
  })

  it('insertAtPath：插入数组（索引钳制）', () => {
    const next = insertAtPath(sample, ['loop_bodies', 1, 'steps'], 1, { id: 'mid', steps: [] })
    expect(next.loop_bodies[1].steps.map((s) => s.id)).toEqual(['prepare', 'mid', 'post'])
    // 超界索引落末尾
    const tail = insertAtPath(sample, ['loop_bodies', 1, 'steps'], 99, { id: 'tail', steps: [] })
    expect(tail.loop_bodies[1].steps.at(-1).id).toBe('tail')
  })

  it('moveArrayItem：相邻移动与越界不动', () => {
    const up = moveArrayItem(sample, ['loop_bodies', 1, 'steps'], 1, -1)
    expect(up.loop_bodies[1].steps.map((s) => s.id)).toEqual(['post', 'prepare'])
    const noop = moveArrayItem(sample, ['loop_bodies', 1, 'steps'], 0, -1)
    expect(noop).toBe(sample)
  })

  it('模型外未知字段在更新后保真', () => {
    const next = insertAtPath(sample, ['loop_bodies', 0, 'steps'], 0, { id: 'x', steps: [] })
    expect(next.custom_top_level).toBe('keep-me')
    expect(next.name).toBe('autonomous')
  })

  it('G10 DSL 字段在更新后保真（while / next / G9 门条目）', () => {
    const next = setAtPath(sample, ['loop_bodies', 0, 'run_on_error'], true)
    expect(next.loop_bodies[1].while).toBe('True')
    expect(next.loop_bodies[1].steps[1].next).toEqual([{ when: 'True', then: 'end' }])
    expect(next.loop_bodies[1].steps[0].steps[1]).toBe('{{state.core_plugin}}')
  })
})
