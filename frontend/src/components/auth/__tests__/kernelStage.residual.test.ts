/** @feature FP-T12 前端组件补测 | @ci: frontend-test */
/**
 * kernelStage 文案映射补测：启动失败阶段标签（解压 / 内核拉起）与未知阶段
 * 的兜底文案——失败面文案是用户唯一可见的内核启动诊断入口，未知阶段也必须
 * 有可读标签（不能出现空串/undefined）。
 */

import { describe, expect, it } from 'vitest'
import { bootStageLabel } from '../kernelStage'

describe('bootStageLabel', () => {
  it('已知阶段：解压 / 环境装配 / 内核拉起', () => {
    expect(bootStageLabel('extract')).toBe('解压')
    expect(bootStageLabel('assemble')).toBe('环境装配')
    expect(bootStageLabel('starting')).toBe('内核拉起')
  })

  it('阶段缺失或未知：统一兜底为「启动」（两档输入可区分空串与未知值）', () => {
    expect(bootStageLabel(undefined)).toBe('启动')
    expect(bootStageLabel('')).toBe('启动')
    expect(bootStageLabel('future_stage')).toBe('启动')
  })
})
