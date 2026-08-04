/**
 * resolvePipelineId 测试 - 流式事件路由解析
 *
 * 验证：pipeline_id 严格校验（空字符串视为无效）
 */
import { describe, it, expect } from 'vitest'
import { resolvePipelineId } from '../router'

describe('resolvePipelineId', () => {
  it('正常 pipeline_id 返回原值', () => {
    const result = resolvePipelineId({ data: { pipeline_id: 'pipe-123' } })
    expect(result).toBe('pipe-123')
  })

  it('pipeline_id 为空字符串时返回 null', () => {
    const result = resolvePipelineId({ data: { pipeline_id: '' } })
    expect(result).toBeNull()
  })

  it('pipeline_id 缺失时返回 null', () => {
    const result = resolvePipelineId({ data: {} })
    expect(result).toBeNull()
  })

  it('pipeline_id 为 null 时返回 null', () => {
    const result = resolvePipelineId({ data: { pipeline_id: null } })
    expect(result).toBeNull()
  })

  it('pipeline_id 为 undefined 时返回 null', () => {
    const result = resolvePipelineId({ data: { pipeline_id: undefined } })
    expect(result).toBeNull()
  })

  it('data 层缺失时返回 null', () => {
    const result = resolvePipelineId({})
    expect(result).toBeNull()
  })

  it('pipeline_id 为非字符串类型时返回 null', () => {
    const result = resolvePipelineId({ data: { pipeline_id: 123 } })
    expect(result).toBeNull()
  })

  it('eventData 为 null 时抛出 TypeError', () => {
    expect(() => resolvePipelineId(null)).toThrow(TypeError)
  })

  it('不使用 thread_id 作为 fallback', () => {
    const result = resolvePipelineId({ data: { pipeline_id: '' }, _threadId: 'thread-1' })
    expect(result).toBeNull()
  })
})
