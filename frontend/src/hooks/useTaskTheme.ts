/**
 * 任务主题 Hook
 *
 * 提供任务相关的主题颜色和样式
 * 自动适配深色和浅色主题
 */

import { useMemo } from 'react'
import { useThemeStore } from '@/stores/themeStore'
import type {
  TaskStatus,
  TaskPhase,
  ACStatus,
  TaskType,
  AgentLevel,
  PhaseStatusType,
} from '@/types'

/**
 * 任务主题 Hook 返回值
 */
export interface TaskTheme {
  // 任务状态颜色
  getTaskStatusColor: (status: TaskStatus) => string
  getTaskStatusBgColor: (status: TaskStatus) => string
  getTaskStatusTextColor: (status: TaskStatus) => string

  // 阶段颜色
  getPhaseColor: (phase: TaskPhase) => string
  getPhaseStatusColor: (status: PhaseStatusType) => string
  getPhaseStatusBgColor: (status: PhaseStatusType) => string
  getPhaseStatusTextColor: (status: PhaseStatusType) => string

  // AC 状态颜色
  getACStatusColor: (status: ACStatus) => string
  getACStatusBgColor: (status: ACStatus) => string
  getACStatusTextColor: (status: ACStatus) => string

  // 任务类型颜色
  getTaskTypeColor: (type: TaskType) => string
  getTaskTypeBgColor: (type: TaskType) => string
  getTaskTypeTextColor: (type: TaskType) => string

  // Agent 层级颜色
  getAgentLevelColor: (level: AgentLevel) => string
  getAgentLevelBgColor: (level: AgentLevel) => string
  getAgentLevelTextColor: (level: AgentLevel) => string

  // 进度条颜色（根据完成度）
  getProgressColor: (progress: number) => string

  // AC 进度颜色（根据通过率）
  getACProgressColor: (passed: number, total: number) => string
  getACProgressBgColor: (passed: number, total: number) => string
  getACProgressTextColor: (passed: number, total: number) => string

  // 主题变量（直接使用 CSS 变量）
  cssVars: {
    taskPending: string
    taskInProgress: string
    taskCompleted: string
    taskFailed: string
    taskBlocked: string
    phasePrepare: string
    phaseExecute: string
    phaseEvaluate: string
    acPending: string
    acEvaluating: string
    acPassed: string
    acFailed: string
    taskTypePlanning: string
    taskTypeExecution: string
    taskTypeFinalEvaluation: string
    agentLevelL1: string
    agentLevelL2: string
    agentLevelL3: string
  }
}

/**
 * 默认颜色值（深色主题）
 */
const DEFAULT_COLORS = {
  task: {
    pending: '#6b7280',
    in_progress: '#3b82f6',
    completed: '#10b981',
    failed: '#ef4444',
    blocked: '#f59e0b',
  },
  phase: {
    prepare: '#3b82f6',
    execute: '#10b981',
    evaluate: '#8b5cf6',
  },
  acceptance: {
    pending: '#6b7280',
    evaluating: '#3b82f6',
    passed: '#10b981',
    failed: '#ef4444',
  },
  task_type: {
    planning: '#8b5cf6',
    execution: '#f59e0b',
    final_evaluation: '#ec4899',
  },
  agent_level: {
    l1: '#6366f1',
    l2: '#8b5cf6',
    l3: '#a855f7',
  },
}

/**
 * 获取颜色的半透明背景色版本
 */
function getBgColor(hex: string, opacity: number = 0.1): string {
  // 移除 # 号
  const cleanHex = hex.replace('#', '')

  // 转换为 RGB
  const r = Number.parseInt(cleanHex.substring(0, 2), 16)
  const g = Number.parseInt(cleanHex.substring(2, 4), 16)
  const b = Number.parseInt(cleanHex.substring(4, 6), 16)

  return `rgba(${r}, ${g}, ${b}, ${opacity})`
}

/**
 * 任务主题 Hook
 */
export function useTaskTheme(): TaskTheme {
  const { themeConfig, resolvedTheme } = useThemeStore()

  // 获取主题颜色（带默认值）
  const colors = useMemo(() => {
    if (!themeConfig?.colors) {
      return DEFAULT_COLORS
    }

    return {
      task: {
        pending: themeConfig.colors.task?.pending || DEFAULT_COLORS.task.pending,
        in_progress: themeConfig.colors.task?.in_progress || DEFAULT_COLORS.task.in_progress,
        completed: themeConfig.colors.task?.completed || DEFAULT_COLORS.task.completed,
        failed: themeConfig.colors.task?.failed || DEFAULT_COLORS.task.failed,
        blocked: themeConfig.colors.task?.blocked || DEFAULT_COLORS.task.blocked,
      },
      phase: {
        prepare: themeConfig.colors.phase?.prepare || DEFAULT_COLORS.phase.prepare,
        execute: themeConfig.colors.phase?.execute || DEFAULT_COLORS.phase.execute,
        evaluate: themeConfig.colors.phase?.evaluate || DEFAULT_COLORS.phase.evaluate,
      },
      acceptance: {
        pending: themeConfig.colors.acceptance?.pending || DEFAULT_COLORS.acceptance.pending,
        evaluating:
          themeConfig.colors.acceptance?.evaluating || DEFAULT_COLORS.acceptance.evaluating,
        passed: themeConfig.colors.acceptance?.passed || DEFAULT_COLORS.acceptance.passed,
        failed: themeConfig.colors.acceptance?.failed || DEFAULT_COLORS.acceptance.failed,
      },
      task_type: {
        planning: themeConfig.colors.task_type?.planning || DEFAULT_COLORS.task_type.planning,
        execution: themeConfig.colors.task_type?.execution || DEFAULT_COLORS.task_type.execution,
        final_evaluation:
          themeConfig.colors.task_type?.final_evaluation ||
          DEFAULT_COLORS.task_type.final_evaluation,
      },
      agent_level: {
        l1: themeConfig.colors.agent_level?.l1 || DEFAULT_COLORS.agent_level.l1,
        l2: themeConfig.colors.agent_level?.l2 || DEFAULT_COLORS.agent_level.l2,
        l3: themeConfig.colors.agent_level?.l3 || DEFAULT_COLORS.agent_level.l3,
      },
    }
  }, [themeConfig])

  // CSS 变量
  const cssVars = useMemo(
    () => ({
      taskPending: `var(--task-pending, ${colors.task.pending})`,
      taskInProgress: `var(--task-in-progress, ${colors.task.in_progress})`,
      taskCompleted: `var(--task-completed, ${colors.task.completed})`,
      taskFailed: `var(--task-failed, ${colors.task.failed})`,
      taskBlocked: `var(--task-blocked, ${colors.task.blocked})`,
      phasePrepare: `var(--phase-prepare, ${colors.phase.prepare})`,
      phaseExecute: `var(--phase-execute, ${colors.phase.execute})`,
      phaseEvaluate: `var(--phase-evaluate, ${colors.phase.evaluate})`,
      acPending: `var(--ac-pending, ${colors.acceptance.pending})`,
      acEvaluating: `var(--ac-evaluating, ${colors.acceptance.evaluating})`,
      acPassed: `var(--ac-passed, ${colors.acceptance.passed})`,
      acFailed: `var(--ac-failed, ${colors.acceptance.failed})`,
      taskTypePlanning: `var(--task-type-planning, ${colors.task_type.planning})`,
      taskTypeExecution: `var(--task-type-execution, ${colors.task_type.execution})`,
      taskTypeFinalEvaluation: `var(--task-type-final-evaluation, ${colors.task_type.final_evaluation})`,
      agentLevelL1: `var(--agent-level-l1, ${colors.agent_level.l1})`,
      agentLevelL2: `var(--agent-level-l2, ${colors.agent_level.l2})`,
      agentLevelL3: `var(--agent-level-l3, ${colors.agent_level.l3})`,
    }),
    [colors],
  )

  // 任务状态颜色
  const getTaskStatusColor = (status: TaskStatus): string => {
    switch (status) {
      case 'pending':
        return colors.task.pending
      case 'in_progress':
        return colors.task.in_progress
      case 'completed':
        return colors.task.completed
      case 'failed':
        return colors.task.failed
      case 'blocked':
        return colors.task.blocked
      default:
        return colors.task.pending
    }
  }

  const getTaskStatusBgColor = (status: TaskStatus): string => {
    return getBgColor(getTaskStatusColor(status), resolvedTheme === 'dark' ? 0.2 : 0.1)
  }

  const getTaskStatusTextColor = (status: TaskStatus): string => {
    return getTaskStatusColor(status)
  }

  // 阶段颜色
  const getPhaseColor = (phase: TaskPhase): string => {
    switch (phase) {
      case 'prepare':
        return colors.phase.prepare
      case 'execute':
        return colors.phase.execute
      case 'evaluate':
        return colors.phase.evaluate
      default:
        return colors.phase.prepare
    }
  }

  const getPhaseStatusColor = (status: PhaseStatusType): string => {
    switch (status) {
      case 'pending':
        return colors.task.pending
      case 'running':
        return colors.task.in_progress
      case 'completed':
        return colors.task.completed
      case 'failed':
        return colors.task.failed
      default:
        return colors.task.pending
    }
  }

  const getPhaseStatusBgColor = (status: PhaseStatusType): string => {
    return getBgColor(getPhaseStatusColor(status), resolvedTheme === 'dark' ? 0.2 : 0.1)
  }

  const getPhaseStatusTextColor = (status: PhaseStatusType): string => {
    return getPhaseStatusColor(status)
  }

  // AC 状态颜色
  const getACStatusColor = (status: ACStatus): string => {
    switch (status) {
      case 'pending':
        return colors.acceptance.pending
      case 'evaluating':
        return colors.acceptance.evaluating
      case 'passed':
        return colors.acceptance.passed
      case 'failed':
        return colors.acceptance.failed
      default:
        return colors.acceptance.pending
    }
  }

  const getACStatusBgColor = (status: ACStatus): string => {
    return getBgColor(getACStatusColor(status), resolvedTheme === 'dark' ? 0.2 : 0.1)
  }

  const getACStatusTextColor = (status: ACStatus): string => {
    return getACStatusColor(status)
  }

  // 任务类型颜色
  const getTaskTypeColor = (type: TaskType): string => {
    switch (type) {
      case 'planning':
        return colors.task_type.planning
      case 'execution':
        return colors.task_type.execution
      case 'final_evaluation':
        return colors.task_type.final_evaluation
      default:
        return colors.task_type.planning
    }
  }

  const getTaskTypeBgColor = (type: TaskType): string => {
    return getBgColor(getTaskTypeColor(type), resolvedTheme === 'dark' ? 0.2 : 0.1)
  }

  const getTaskTypeTextColor = (type: TaskType): string => {
    return getTaskTypeColor(type)
  }

  // Agent 层级颜色
  const getAgentLevelColor = (level: AgentLevel): string => {
    switch (level) {
      case 1:
        return colors.agent_level.l1
      case 2:
        return colors.agent_level.l2
      case 3:
        return colors.agent_level.l3
      default:
        return colors.agent_level.l1
    }
  }

  const getAgentLevelBgColor = (level: AgentLevel): string => {
    return getBgColor(getAgentLevelColor(level), resolvedTheme === 'dark' ? 0.2 : 0.1)
  }

  const getAgentLevelTextColor = (level: AgentLevel): string => {
    return getAgentLevelColor(level)
  }

  // 进度条颜色（根据完成度）
  const getProgressColor = (progress: number): string => {
    if (progress >= 100) return colors.task.completed
    if (progress >= 60) return colors.task.in_progress
    if (progress >= 30) return colors.phase.prepare
    return colors.task.pending
  }

  // AC 进度颜色（根据通过率）
  const getACProgressColor = (passed: number, total: number): string => {
    if (total === 0) return colors.acceptance.pending
    const percentage = (passed / total) * 100
    if (percentage === 100) return colors.acceptance.passed
    if (percentage >= 50) return colors.phase.execute
    return colors.acceptance.pending
  }

  // AC 进度背景色
  const getACProgressBgColor = (passed: number, total: number): string => {
    return getBgColor(getACProgressColor(passed, total), resolvedTheme === 'dark' ? 0.2 : 0.1)
  }

  // AC 进度文字色
  const getACProgressTextColor = (passed: number, total: number): string => {
    return getACProgressColor(passed, total)
  }

  return {
    getTaskStatusColor,
    getTaskStatusBgColor,
    getTaskStatusTextColor,
    getPhaseColor,
    getPhaseStatusColor,
    getPhaseStatusBgColor,
    getPhaseStatusTextColor,
    getACStatusColor,
    getACStatusBgColor,
    getACStatusTextColor,
    getTaskTypeColor,
    getTaskTypeBgColor,
    getTaskTypeTextColor,
    getAgentLevelColor,
    getAgentLevelBgColor,
    getAgentLevelTextColor,
    getProgressColor,
    getACProgressColor,
    getACProgressBgColor,
    getACProgressTextColor,
    cssVars,
  }
}

export default useTaskTheme
