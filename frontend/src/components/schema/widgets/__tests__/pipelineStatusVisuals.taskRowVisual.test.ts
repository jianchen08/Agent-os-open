// @feature FP-T12 前端适配 | @ci: frontend-test
// @ci: frontend-test
/**
 * taskRowStatusVisual 单测（任务行显示聚合视觉，§4.5 + 既有防御不回退）：
 * - 折算直出（等待子任务 / 已暂停（可恢复））；
 * - BUG-2b 终态防御：折算运行中 × 运行态已终 → 终态直出，不复活幽灵执行中；
 *   cancelled 终态按过渡映射落已暂停（已取消退役）；
 * - BUG-21 无证据防御：折算运行中 × 运行态未知/缺席 → 未知，不猜 running；
 * - task.status 裁决权威：终态折算不被非终运行态覆盖。
 */
import { describe, expect, it } from 'vitest'
import { taskRowStatusVisual } from '../pipelineStatusVisuals'

describe('taskRowStatusVisual：任务行显示聚合', () => {
  it('折算直出：stopped+挂号 → 等待子任务；stopped 无挂号 → 已暂停（可恢复）', () => {
    expect(taskRowStatusVisual('stopped', true, 'suspended')).toEqual({
      color: 'text-status-info',
      label: '等待子任务',
    })
    expect(taskRowStatusVisual('stopped', false, 'suspended')).toEqual({
      color: 'text-status-pending',
      label: '已暂停（可恢复）',
    })
  })

  it('BUG-2b 终态防御：折算运行中 × 运行态已终 → 终态直出不复活幽灵执行中', () => {
    expect(taskRowStatusVisual('running', false, 'completed')).toEqual({
      color: 'text-status-success',
      label: '已完成',
    })
    expect(taskRowStatusVisual('running', false, 'failed')).toEqual({
      color: 'text-status-error',
      label: '失败',
    })
  })

  it('BUG-2b × cancelled 终态：过渡映射落已暂停（已取消自任务面板退役）', () => {
    expect(taskRowStatusVisual('running', false, 'cancelled')).toEqual({
      color: 'text-status-pending',
      label: '已暂停（可恢复）',
    })
  })

  it('BUG-21 无证据防御：折算运行中 × 运行态未知/缺席 → 未知不猜 running', () => {
    expect(taskRowStatusVisual('running', false, 'unknown')).toEqual({
      color: 'text-status-pending',
      label: '未知',
    })
    expect(taskRowStatusVisual('running', false, undefined)).toEqual({
      color: 'text-status-pending',
      label: '未知',
    })
  })

  it('task.status 裁决权威：终态折算不被非终运行态覆盖（运行中 run × stopped 任务 → 已暂停）', () => {
    expect(taskRowStatusVisual('stopped', false, 'running')).toEqual({
      color: 'text-status-pending',
      label: '已暂停（可恢复）',
    })
    expect(taskRowStatusVisual('completed', false, 'suspended')).toEqual({
      color: 'text-status-success',
      label: '已完成',
    })
  })
})
