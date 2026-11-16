/**
 * 会话按工作空间聚合（纯函数，SessionList 会话树数据源）
 *
 * 语义：显式选择了工作空间目录（thread_fields workspace 字段，metadata.workspace）
 * 的会话按目录聚合成会话树；未填工作空间（默认空间）的会话不参与聚合，
 * 由调用方留在平铺列表。
 */
import { sanitizeSessionTitle } from '@/utils/sessionTitle'
import type { Session } from '@/types/models'

/** 一棵会话树：同一工作空间目录下的全部会话（置顶/星标只作视觉标记，照常入树） */
export interface WorkspaceSessionGroup {
  /** 聚合键：workspace 去尾部路径分隔符（'D:/proj/' 与 'D:/proj' 同目录同组） */
  key: string
  /** 顶栏展示路径 = 组内最新会话的原始 workspace 值（排序后取值，结果确定） */
  workspace: string
  /** 顶栏封面名：路径末段（目录名） */
  name: string
  /** 组内会话，updatedAt 降序 */
  sessions: Session[]
}

/** 会话是否绑定了显式工作空间（留空/空白 = 默认空间） */
export function hasExplicitWorkspace(session: Session): boolean {
  return typeof session.workspace === 'string' && session.workspace.trim() !== ''
}

/** 聚合键：去首尾空白与尾部路径分隔符 */
function workspaceKeyOf(workspace: string): string {
  return workspace.trim().replace(/[\\/]+$/, '')
}

/** 路径末段作封面名（分隔符耗尽时回退整段路径） */
export function workspaceNameOf(workspace: string): string {
  const trimmed = workspace.trim()
  return trimmed.split(/[\\/]/).filter(Boolean).pop() || trimmed
}

/**
 * 按工作空间聚合会话：组内 updatedAt 降序；组间按组内最新会话 updatedAt 降序。
 * 非显式工作空间会话直接忽略（默认空间不聚合，由调用方平铺渲染）。
 */
export function groupSessionsByWorkspace(sessions: Session[]): WorkspaceSessionGroup[] {
  const byKey = new Map<string, WorkspaceSessionGroup>()
  for (const session of sessions) {
    if (!hasExplicitWorkspace(session)) continue
    const key = workspaceKeyOf(session.workspace as string)
    let group = byKey.get(key)
    if (!group) {
      group = { key, workspace: session.workspace as string, name: workspaceNameOf(session.workspace as string), sessions: [] }
      byKey.set(key, group)
    }
    group.sessions.push(session)
  }

  const recency = (s: Session) => new Date(s.updatedAt || s.createdAt).getTime()
  const groups = Array.from(byKey.values())
  for (const group of groups) {
    group.sessions.sort((a, b) => recency(b) - recency(a))
    // 展示路径取组内最新会话的原始值：同组多写法（尾分隔符差异）时结果确定
    group.workspace = group.sessions[0].workspace as string
    group.name = workspaceNameOf(group.workspace)
  }
  groups.sort((a, b) => recency(b.sessions[0]) - recency(a.sessions[0]))
  return groups
}

/** 条目标题兜底占位符（与 mappers.mapThreadToSession 的未命名会话文案同源） */
const UNTITLED_SESSION_TITLE = '未命名会话'

/**
 * 会话条目展示标题（纯函数，条目标题取值的单一真值源）
 *
 * 取值优先级：会话自身标题 > 工作空间名（路径末段）> 占位符。挂靠项目/文件夹的
 * 会话始终显示自己的标题——工作空间名只在会话无标题时兜底，否则条目看上去
 * "名为工作空间名"。标题先经展示净化再判空：净化后为空串/仅空白视同无标题。
 */
export function sessionRowTitleOf(session: Session): string {
  const title = sanitizeSessionTitle(session.title ?? '').trim()
  if (title) return title
  const workspace = session.workspace ? workspaceNameOf(session.workspace).trim() : ''
  return workspace || UNTITLED_SESSION_TITLE
}
