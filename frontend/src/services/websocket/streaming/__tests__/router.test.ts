/**
 * resolvePipelineId 测试 - 流式事件路由解析
 *
 * 验证：pipeline_id 严格校验（空字符串视为无效）
 */
import { describe, it, expect } from 'vitest'
import { usePipelineMessageStore } from '@/stores/pipelineMessageStore'
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

  // ADR 2026-08-21：redirect（未注册管道重定向到活跃管道）已删除——
  // spike 实证后端 resolve_pipeline_id_for_thread 保证回流事件 pipeline_id 恒为
  // 前端注册值，重定向防御场景不存在；未注册管道事件由 isPipelineRelevant 门控丢弃。
  it('未注册管道的 pipeline_id 原样返回（不重定向到活跃管道）', () => {
    usePipelineMessageStore.setState({
      activePipelineId: 'pipe-active',
      streamingState: { 'pipe-active': { isStreaming: true, messageId: 'm1' } },
      pipelineSessionMap: { 'pipe-active': 'thread-1' },
      pipelines: {},
    })
    const result = resolvePipelineId({
      data: { pipeline_id: 'pipe-unregistered', _threadId: 'thread-1' },
    })
    expect(result).toBe('pipe-unregistered')
  })
})
