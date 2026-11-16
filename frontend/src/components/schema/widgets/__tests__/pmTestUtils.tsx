/**
 * PipelineManagerWidget 测试共享夹具：播种单例 + mock 模块造型工厂 + 公共渲染序列。
 *
 * 职责边界（vi.mock 提升语义不可跨文件共享）：mock 声明（vi.mock 调用）必须留在
 * 各测试文件——本文件不出现 vi.mock，只提供工厂；各测试文件的 vi.mock 工厂体以
 * 单行委托引用这里的造型，消除逐行复制的 mock 模块装配。
 *
 * 隔离模型：vitest 每测试文件独立模块注册表，pmSeed 单例天然按文件隔离；容器在
 * beforeEach 用 resetPmContainers 清空。引用顺序约束：测试文件须先 import 本文件
 * 再 import 被测组件——vi.mock 工厂体在被测组件初始化时才执行，届时本模块必须
 * 已完成求值。
 */

import { fireEvent, screen } from '@testing-library/react'
import { vi } from 'vitest'
import { renderWithProviders } from '@/test/renderWithProviders'
import type { ReactElement } from 'react'

/** 播种容器（可变单例：beforeEach 清空，用例自行填充） */
const runs: Record<string, Record<string, unknown>> = {}
const states: Record<string, Record<string, unknown>> = {}
const tasks: Record<string, unknown>[] = []
const sessions: Record<string, unknown>[] = []
const projects: Record<string, unknown>[] = []
const agents: Record<string, unknown>[] = []
const usage = { usageByPipeline: {} as Record<string, Record<string, unknown>> }

/** modes registry 合成体（模式徽标名/图标数据源；按用例可变） */
export const modesRegistry: {
  modes: Array<Record<string, unknown>>
  total: number
  errors: string[]
} = { modes: [], total: 0, errors: [] }

/**
 * 播种数据 + 可断言 mock 句柄。默认实现对齐各 query hook 的宽松形态
 * （数据容器缺省为空），需要特定返回形态的用例用 mockReturnValue 覆写。
 */
export const pmSeed = {
  runs,
  states,
  tasks,
  sessions,
  projects,
  agents,
  usage,
  modesRegistry,
  pauseTask: vi.fn(() => Promise.resolve()),
  resumeTask: vi.fn(() => Promise.resolve()),
  cancelTask: vi.fn(() => Promise.resolve()),
  deleteProject: vi.fn(() =>
    Promise.resolve({
      message: '项目已删除',
      id: 'proj-x',
      suspended_children: 0,
      deleted_children: 0,
      folder_removed: false,
    }),
  ),
  fetchProjects: vi.fn(() => Promise.resolve({ items: [] as unknown[] })),
  workspaceOpen: vi.fn(() => Promise.resolve({ data: { success: true } })),
  navigateToPipeline: vi.fn(() => Promise.resolve(true)),
  invalidateLongTermTasks: vi.fn(),
  readSessions: vi.fn(() => [] as Record<string, unknown>[]),
  ensureSessionsLoaded: vi.fn(() => Promise.resolve([] as unknown[])),
  setActiveSession: vi.fn(() => Promise.resolve()),
  rebuildSessionFromThread: vi.fn(),
  usePipelineRunsQuery: vi.fn(() => ({ data: runs })),
  usePipelineStatesQuery: vi.fn(() => ({ data: states })),
  useAllTasksQuery: vi.fn(() => ({ data: tasks })),
  useSessionsQuery: vi.fn(() => ({ data: sessions })),
  useAgentsQuery: vi.fn(() => ({ data: agents })),
}

/** 清空全部播种容器（mock 句柄的默认实现保留，需要重配的用例自行 mockReset） */
export function resetPmContainers() {
  for (const k of Object.keys(pmSeed.runs)) delete pmSeed.runs[k]
  for (const k of Object.keys(pmSeed.states)) delete pmSeed.states[k]
  pmSeed.tasks.length = 0
  pmSeed.sessions.length = 0
  pmSeed.projects.length = 0
  pmSeed.agents.length = 0
  for (const k of Object.keys(pmSeed.usage.usageByPipeline)) {
    delete pmSeed.usage.usageByPipeline[k]
  }
}

/** widget 依赖模块的 mock 造型（各测试文件 vi.mock 工厂体单行委托） */
export const pmMod = {
  /** 任务域端点：暂停/恢复/取消/删除项目/项目登记拉取 */
  tasksApi: () => ({
    pauseTask: pmSeed.pauseTask,
    resumeTask: pmSeed.resumeTask,
    cancelTask: pmSeed.cancelTask,
    deleteProject: pmSeed.deleteProject,
    fetchProjects: pmSeed.fetchProjects,
  }),
  /** workspaces open 端点（项目文件夹打开） */
  apiClient: () => ({ default: { post: pmSeed.workspaceOpen } }),
  pipelineNavigator: () => ({ navigateToPipeline: pmSeed.navigateToPipeline }),
  pipelineRunsQuery: () => ({
    usePipelineRunsQuery: pmSeed.usePipelineRunsQuery,
    usePipelineStatesQuery: pmSeed.usePipelineStatesQuery,
  }),
  allTasksQuery: () => ({ useAllTasksQuery: pmSeed.useAllTasksQuery }),
  longTermTasksQuery: () => ({ invalidateLongTermTasks: pmSeed.invalidateLongTermTasks }),
  sessionsQuery: () => ({
    useSessionsQuery: pmSeed.useSessionsQuery,
    readSessions: pmSeed.readSessions,
    ensureSessionsLoaded: pmSeed.ensureSessionsLoaded,
  }),
  agentsQuery: () => ({ useAgentsQuery: pmSeed.useAgentsQuery }),
  contextUsageStore: () => ({
    useContextUsageStore: (sel: (s: unknown) => unknown) =>
      sel({ usageByPipeline: pmSeed.usage.usageByPipeline }),
  }),
  sessionListStore: () => ({
    useSessionListStore: {
      getState: () => ({
        setActiveSession: pmSeed.setActiveSession,
        rebuildSessionFromThread: pmSeed.rebuildSessionFromThread,
      }),
    },
  }),
}

/**
 * 渲染并切到全量视野：状态组无「全部」项，标签全不选中即全量——无活跃会话时
 * 面板缺省选中「运行中」，点该标签取消即全量；有活跃会话时缺省已空集，
 * 无需点击（缺省契约见 PipelineManagerWidget 状态筛选注）。
 *
 * 会话激活与否以会话 chip 是否已渲染为准（render 同步 flush effects，chip
 * 在 ⇔ 挂载时有活跃会话）；不顶层 import sessionStore——它经 GlobalWebSocket
 * 拉到被 mock 的 api/client，vi.mock 工厂会在本模块求值期触发 pmMod TDZ
 * （见各测试文件 mock 时序约束）。
 */
export function renderPmAllStatuses(ui: ReactElement) {
  const ret = renderWithProviders(ui)
  if (!screen.queryByLabelText('移除会话筛选')) {
    fireEvent.click(screen.getByRole('button', { name: '状态：运行中' }))
  }
  return ret
}
