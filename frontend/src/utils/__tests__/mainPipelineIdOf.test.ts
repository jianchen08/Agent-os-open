/** @feature FP-T12 前端适配 | @ci frontend-test */
/**
 * mainPipelineIdOf 主管道权威解析测试（2026-08-22 裁决）
 *
 * 反模式收口批次3：消费端原先用 pipelineIds[0] 位置序号当主管道——
 * 后端权威 active_pipeline_id 在场不用，排序不保证主管道在前时
 * 消息发进错误管道。新规则：activePipelineId 优先；缺失且恰一个
 * 管道才取 [0]（无歧义）；缺失且多元素 → undefined（不猜，fail-closed）。
 */
import { describe, expect, it } from 'vitest'
import { mainPipelineIdOf } from '@/utils/mappers'

describe('mainPipelineIdOf 主管道权威解析', () => {
  it('activePipelineId 优先——pipelineIds 顺序相反也不受影响', () => {
    expect(
      mainPipelineIdOf({ activePipelineId: 'P-main', pipelineIds: ['P-sub', 'P-main'] }),
    ).toBe('P-main')
  })

  it('activePipelineId 缺失且恰一个管道 → 取唯一元素（无歧义）', () => {
    expect(mainPipelineIdOf({ activePipelineId: null, pipelineIds: ['P-only'] })).toBe('P-only')
  })

  it('activePipelineId 缺失且多管道 → undefined（不猜位置序号）', () => {
    expect(mainPipelineIdOf({ activePipelineId: null, pipelineIds: ['P1', 'P2'] })).toBeUndefined()
  })

  it('activePipelineId 缺失且无管道 → undefined', () => {
    expect(mainPipelineIdOf({ activePipelineId: undefined, pipelineIds: [] })).toBeUndefined()
    expect(mainPipelineIdOf({})).toBeUndefined()
  })
})