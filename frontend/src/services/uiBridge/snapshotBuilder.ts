/**
 * 侧边栏快照拼装（冻结件 §2.3 / §2.1）
 *
 * 纯函数：声明面（BUILTIN_SIDEBAR_ITEMS + activity-bar 插件贡献页）与状态面
 * （store 视图态 + 会话列表）拼接为 §2.1 快照体（除 client_id/ts 外全部字段，
 * snake_case）。超限截断在拼装处收口（items≤100 / sessions≤50，§四 边界表
 * 「前端截断」侧；插件侧超限 400 拒收为双保险第二刀）。
 *
 * @feature FP-0.2.侧边栏UI对象化 侧边栏UI对象化P0感知闭环 | @vision V2 全能闭环 | @ci frontend-test
 */
import { BUILTIN_SIDEBAR_ITEMS } from './builtinItems'
import type { SidebarItemDecl } from './builtinItems'
import type { PageDeclaration } from '@/services/schema/ContributionRegistry'

/** 快照输入（camelCase 前端侧形态；snake_case 输出在 buildSidebarSnapshot 归一） */
export interface SidebarSnapshotInput {
  collapsed: boolean
  widthRatio: number
  activeView: string
  unreadCount: number
  /** slot==='activity-bar' 的插件贡献页 */
  pluginPages: PageDeclaration[]
  /** 会话列表（上限 50 截断） */
  sessions: Array<{ id: string; title: string; pinned: boolean; active: boolean }>
}

/** 快照输出 = §2.1 请求体除 client_id/ts 外全部字段 */
export interface SidebarSnapshot {
  state: {
    collapsed: boolean
    width_ratio: number
    active_view: string
    unread_count: number
  }
  items: SidebarItemDecl[]
  sessions: Array<{ id: string; title: string; pinned: boolean; active: boolean }>
  actions: string[]
}

const MAX_ITEMS = 100
const MAX_SESSIONS = 50

/** P0 支持的操作词表（封闭 4 项，§2.1 actions；命令词表消费在 P1） */
const ACTIONS_VOCAB = ['navigate', 'set_collapsed', 'open_page', 'highlight'] as const

export function buildSidebarSnapshot(input: SidebarSnapshotInput): SidebarSnapshot {
  const pluginItems: SidebarItemDecl[] = input.pluginPages.map((page) => ({
    id: page.id,
    kind: 'plugin_page',
    title: page.title ?? page.id,
    order: typeof page.order === 'number' ? page.order : 0,
    visible: true,
  }))
  return {
    state: {
      collapsed: input.collapsed,
      width_ratio: input.widthRatio,
      active_view: input.activeView,
      unread_count: input.unreadCount,
    },
    items: [...BUILTIN_SIDEBAR_ITEMS, ...pluginItems].slice(0, MAX_ITEMS),
    sessions: input.sessions.slice(0, MAX_SESSIONS),
    actions: [...ACTIONS_VOCAB],
  }
}
