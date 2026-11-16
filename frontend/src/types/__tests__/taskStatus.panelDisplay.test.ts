// @feature FP-T12 前端适配 | @ci: frontend-test
// @ci: frontend-test
/**
 * 任务面板显示聚合单测（设计 §4.5 一词一义，ADR 2026-10-30-task-status-authority-
 * vocabulary-convergence 决策 7）。
 *
 * taskPanelDisplayKey：(task.status, 子任务挂号非空?) → 显示键折算纯函数，
 * 覆盖设计表全部行 + cancelled 过渡期防御 + 未知值诚实兜底；
 * taskPendingSubtaskParents：state 挂号键（task.subtasks_pending.*）不在面板
 * 数据出口时的父子关系推导（结算集与后端回执写面同源：completed/failed）。
 */
import { describe, expect, it, vi, afterEach } from 'vitest'
import {
  TASK_PANEL_DISPLAY_LABELS,
  taskPanelDisplayKey,
  taskPendingSubtaskParents,
} from '../taskStatus'

describe('taskPanelDisplayKey：§4.5 折算（区分度输入组）', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('running/evaluating → 运行中（挂号有无不参与折算）', () => {
    expect(taskPanelDisplayKey('running', false)).toBe('running')
    expect(taskPanelDisplayKey('running', true)).toBe('running')
    expect(taskPanelDisplayKey('evaluating', false)).toBe('running')
    expect(taskPanelDisplayKey('evaluating', true)).toBe('running')
  })

  it('stopped × 挂号非空 → 等待子任务；× 挂号空 → 已暂停（可恢复）', () => {
    expect(taskPanelDisplayKey('stopped', true)).toBe('waiting_subtasks')
    expect(taskPanelDisplayKey('stopped', false)).toBe('paused')
  })

  it('cancelled 过渡期防御：别名折叠落 stopped，显示同 stopped（legacy：后端开机迁移后该输入消失）', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    expect(taskPanelDisplayKey('cancelled', false)).toBe('paused')
    expect(taskPanelDisplayKey('cancelled', true)).toBe('waiting_subtasks')
    // 别名折叠不告警（合法旧值，非未知词）
    expect(warn).not.toHaveBeenCalled()
  })

  it('failed → 失败；completed → 已完成', () => {
    expect(taskPanelDisplayKey('failed', false)).toBe('failed')
    expect(taskPanelDisplayKey('failed', true)).toBe('failed')
    expect(taskPanelDisplayKey('completed', false)).toBe('completed')
  })

  it('pending/timeout 现有文案保留：面板现状 pending 显运行中、timeout 显失败', () => {
    expect(taskPanelDisplayKey('pending', false)).toBe('running')
    expect(taskPanelDisplayKey('timeout', false)).toBe('failed')
  })

  it('未知值 → unknown（不猜合法态；同一未知值只警告一次）', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    expect(taskPanelDisplayKey('mystery_panel_state', false)).toBe('unknown')
    expect(taskPanelDisplayKey('mystery_panel_state', true)).toBe('unknown')
    const calls = warn.mock.calls.filter((args) => String(args[0]).includes('mystery_panel_state'))
    expect(calls).toHaveLength(1)
  })

  it('缺省/非字符串 → unknown 且不告警（空值非未知词）', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    expect(taskPanelDisplayKey(undefined, false)).toBe('unknown')
    expect(taskPanelDisplayKey(undefined, true)).toBe('unknown')
    expect(warn).not.toHaveBeenCalled()
  })

  it('折算键 → 展示文案表完整（一词一义标签单源，组件不得自备映射）', () => {
    expect(TASK_PANEL_DISPLAY_LABELS.running).toBe('运行中')
    expect(TASK_PANEL_DISPLAY_LABELS.waiting_subtasks).toBe('等待子任务')
    expect(TASK_PANEL_DISPLAY_LABELS.paused).toBe('已暂停（可恢复）')
    expect(TASK_PANEL_DISPLAY_LABELS.failed).toBe('失败')
    expect(TASK_PANEL_DISPLAY_LABELS.completed).toBe('已完成')
    expect(TASK_PANEL_DISPLAY_LABELS.unknown).toBe('未知')
  })
})

describe('taskPendingSubtaskParents：子任务挂号推导', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('未结算直接子任务（running/timeout/未知态）→ 父在集合（回执只对 completed/failed 写）', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const parents = taskPendingSubtaskParents([
      { id: 'p1', status: 'stopped' },
      { id: 'c1', status: 'running', parent_task_id: 'p1' },
      { id: 'p2', status: 'stopped' },
      { id: 'c2', status: 'timeout', parent_task_id: 'p2' },
      { id: 'p3', status: 'stopped' },
      { id: 'c3', status: 'weird_pending_state', parent_task_id: 'p3' },
    ])
    expect(parents.has('p1')).toBe(true)
    // timeout 不派生终态事件 → 无回执 → 挂号未清
    expect(parents.has('p2')).toBe(true)
    // 未知态子任务按未结算保守处理（不伪造「已结算」）
    expect(parents.has('p3')).toBe(true)
    expect(warn).toHaveBeenCalled()
  })

  it('已结算子任务（completed/failed）→ 父不在集合（回执已写，挂号清）', () => {
    const parents = taskPendingSubtaskParents([
      { id: 'p1', status: 'stopped' },
      { id: 'c1', status: 'completed', parent_task_id: 'p1' },
      { id: 'c1b', status: 'failed', parent_task_id: 'p1' },
    ])
    expect(parents.has('p1')).toBe(false)
  })

  it('无 parent / 自挂（parent=id）/ 空串 parent 不入集；缺状态字段按未结算保守处理', () => {
    const parents = taskPendingSubtaskParents([
      { id: 'root', status: 'running' },
      { id: 'orphan-child', status: 'running', parent_task_id: '' },
      { id: 'self', status: 'running', parent_task_id: 'self' },
      { id: 'p9', status: 'stopped' },
      { id: 'c9', parent_task_id: 'p9' },
    ])
    expect(parents.has('root')).toBe(false)
    expect(parents.has('orphan-child')).toBe(false)
    expect(parents.has('self')).toBe(false)
    expect(parents.has('p9')).toBe(true)
  })

  it('camelCase 回退键 parentTaskId 同样识别（任务数据双形态兼容）', () => {
    const parents = taskPendingSubtaskParents([
      { id: 'p-camel', status: 'stopped' },
      { id: 'c-camel', status: 'running', parentTaskId: 'p-camel' },
    ])
    expect(parents.has('p-camel')).toBe(true)
  })
})
