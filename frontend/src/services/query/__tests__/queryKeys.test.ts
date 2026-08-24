/** @feature FP-T12 前端适配 | @ci frontend-test */
/**
 * queryKeys 契约测试
 *
 * queryKey 是缓存条目坐标：同数据异 key = 缓存失效 + 重复请求。
 * 固化各 key 的形状与工厂函数行为，防止调用点手写漂移。
 */
import { describe, it, expect } from 'vitest'
import { queryKeys } from '../queryKeys'

describe('queryKeys', () => {
  it('基础 key 均为单元素只读数组', () => {
    expect(queryKeys.sessions).toEqual(['sessions'])
    expect(queryKeys.agents).toEqual(['agents'])
    expect(queryKeys.schema).toEqual(['schema'])
    expect(queryKeys.plugins).toEqual(['plugins'])
    expect(queryKeys.llmConfig).toEqual(['llm-config'])
  })

  it('pipelineConfig 工厂：参数进 key，不同参数产出不同条目', () => {
    expect(queryKeys.pipelineConfig('main')).toEqual(['pipeline-config', 'main'])
    expect(queryKeys.pipelineConfig('review')).toEqual(['pipeline-config', 'review'])
    expect(queryKeys.pipelineConfig('main')).not.toBe(queryKeys.pipelineConfig('review'))
  })
})
