/**
 * 侧边栏内置项声明表（冻结件 §2.3 / §2.1 items 声明面）
 *
 * 内置项不进 ContributionRegistry（防 Sidebar 经 registry 重复渲染，
 * ADR 2026-11-18-sidebar-ui-object-model）；本表是 Agent 可感知的
 * 内置项声明单源，stateReporter 快照 items 面据此拼装。
 *
 * @feature FP-0.2.侧边栏UI对象化 侧边栏UI对象化P0感知闭环 | @vision V2 全能闭环 | @ci frontend-test
 */

/** 侧边栏条目声明（快照 items[] 元素形态，§2.1 逐字） */
export interface SidebarItemDecl {
  id: string
  kind: 'builtin' | 'plugin_page'
  title: string
  order: number
  visible: boolean
}

/** 内置项恒集：新建会话 + 任务管理（任务管理页归前端自有，不经插件声明） */
export const BUILTIN_SIDEBAR_ITEMS: SidebarItemDecl[] = [
  { id: 'builtin.new_session', kind: 'builtin', title: '新建会话', order: 0, visible: true },
  { id: 'builtin.tasks', kind: 'builtin', title: '任务管理', order: 1, visible: true },
]
