/** @feature FP-T12 前端适配 | @ci frontend-test */
/**
 * mainPipelineIdOf 主管道解析测试
 *
 * 契约：主管道 = pipelineIds[0]（映射单一真值）。后端仅 create_session 写入
 * pipeline_ids（恒主管道），读面并入映射表 id 时保序「主管道在前」；运行时
 * 指针 active_pipeline_id 会被任务出生切走，不参与本解析——对话标签与任务
 * 管理面板同源消费，两视图不允许分叉。
 */
import { describe, expect, it } from 'vitest'
import { mainPipelineIdOf } from '@/utils/mappers'

describe('mainPipelineIdOf 主管道解析（映射单一真值）', () => {
  it('正常形态：取映射首位主管道', () => {
    expect(mainPipelineIdOf({ pipelineIds: ['P-main', 'P-sub'] })).toBe('P-main')
  })

  it('activePipelineId 指向任务管道（指针被出生切换）时不影响主管道身份', () => {
    // 运行时指针不是主管道身份：即使调用方带入陈旧快照的指针值也按映射取
    expect(
      mainPipelineIdOf({
        activePipelineId: 'P-task',
        pipelineIds: ['P-main', 'P-task'],
      } as Parameters<typeof mainPipelineIdOf>[0]),
    ).toBe('P-main')
  })

  it('单元素映射：直接取唯一主管道', () => {
    expect(mainPipelineIdOf({ pipelineIds: ['P-only'] })).toBe('P-only')
  })

  it('映射为空或缺失 → undefined（fail-closed，调用方拒绝/中止）', () => {
    expect(mainPipelineIdOf({ pipelineIds: [] })).toBeUndefined()
    expect(mainPipelineIdOf({})).toBeUndefined()
  })

  it('首位为空串条目 → undefined（不把空值当主管道）', () => {
    expect(mainPipelineIdOf({ pipelineIds: ['', 'P-sub'] })).toBeUndefined()
  })
})
