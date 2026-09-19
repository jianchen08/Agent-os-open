/**
 * 任务域 file_tree 绑定——任务树的启停动作与状态词表经注册缝注入。
 *
 * file_tree 通用件不感知任务域（2026-09-18 三向耦合审查 W3）；本模块由组合根
 * （registerWidgets）挂载副作用注册，迁移的是 FileTreeWidget 原有的
 * pauseTask/resumeTask 调用与任务状态七态词表——文案/归一化的单一真值源
 * 仍是 types/taskStatus。
 */
import { pauseTask, resumeTask } from '@/services/api/tasks'
import { TASK_STATUSES, normalizeTaskStatus, taskStatusLabel } from '@/types/taskStatus'
import { registerFileTreeDomainBinding } from './fileTreeActions'

/** 活跃态集合（running/pending/evaluating）——「仅活跃」筛选与开关受控值同源 */
const ACTIVE_STATUSES = new Set(['running', 'pending', 'evaluating'])

/** 状态显示配置（键 = 任务状态词表七态；旧值经 normalize 折叠） */
const STATUS_CONFIG: Record<string, { icon: string; color: string; label: string }> = {
  pending: { icon: 'clock', color: 'text-status-warning', label: taskStatusLabel('pending') },
  running: { icon: 'play', color: 'text-status-info', label: taskStatusLabel('running') },
  evaluating: { icon: 'loader', color: 'text-status-info', label: taskStatusLabel('evaluating') },
  stopped: { icon: 'pause', color: 'text-status-pending', label: taskStatusLabel('stopped') },
  completed: { icon: 'check', color: 'text-status-success', label: taskStatusLabel('completed') },
  failed: { icon: 'x-circle', color: 'text-status-error', label: taskStatusLabel('failed') },
  timeout: { icon: 'x-circle', color: 'text-status-error', label: taskStatusLabel('timeout') },
}

registerFileTreeDomainBinding({
  id: 'tasks',
  statuses: {
    config: STATUS_CONFIG,
    filterOptions: TASK_STATUSES.map((s) => ({ value: s, label: taskStatusLabel(s) })),
    normalize: (status) => normalizeTaskStatus(status),
    active: ACTIVE_STATUSES,
    defaultFilter: 'running',
  },
  toggleEnabled: async (nodeId, enabled) => {
    if (enabled) await resumeTask(nodeId)
    else await pauseTask(nodeId)
  },
  isEnabled: (status) => ACTIVE_STATUSES.has(normalizeTaskStatus(status)),
  isContainerNode: (node) => node.task_scope === 'container',
})
