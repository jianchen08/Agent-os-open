/** @feature FP-T12 前端适配(B8 管道标签) | @ci: frontend-test */
/** 管道级执行绑定存取——存取清闭环/损坏隔离/id 生成回填。 */
import { beforeEach, describe, expect, it } from 'vitest'
import {
  clearPipelineBinding,
  loadPipelineBinding,
  savePipelineBinding,
} from '../pipelineExecutionOptions'

beforeEach(() => localStorage.clear())

describe('pipelineExecutionOptions — 管道级绑定', () => {
  it('存取闭环：mode/pipelineConfigId/agentId 三件齐全', () => {
    savePipelineBinding('pipe-1', {
      mode: 'roleplay',
      pipelineConfigId: 'roleplay',
      agentId: 'mode_roleplay/luna',
    })
    expect(loadPipelineBinding('pipe-1')).toEqual({
      mode: 'roleplay',
      pipelineConfigId: 'roleplay',
      agentId: 'mode_roleplay/luna',
    })
  })

  it('无记录/损坏 JSON/缺 mode 键 → null（损坏隔离，不抛）', () => {
    expect(loadPipelineBinding('pipe-none')).toBeNull()
    localStorage.setItem('pipeline-exec-options:pipe-bad', '{broken')
    expect(loadPipelineBinding('pipe-bad')).toBeNull()
    localStorage.setItem('pipeline-exec-options:pipe-nomode', '{"foo": 1}')
    expect(loadPipelineBinding('pipe-nomode')).toBeNull()
  })

  it('pipelineId 缺席 → 生成 id 返回（预写形态）', () => {
    const id = savePipelineBinding(undefined, { mode: 'roleplay' })
    expect(id).toBeTruthy()
    expect(loadPipelineBinding(id)).toEqual({ mode: 'roleplay' })
  })

  it('clear 后读回 null', () => {
    savePipelineBinding('pipe-c', { mode: 'roleplay' })
    clearPipelineBinding('pipe-c')
    expect(loadPipelineBinding('pipe-c')).toBeNull()
  })
})
