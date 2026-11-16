/**
 * 会话自定义分组（用户手动建组的文件夹面）
 *
 * 会话侧边栏除按工作空间目录自动聚合外，支持用户自建分组（如「工作」「RP」）
 * 并手动分配会话；分组随会话列表渲染为可折叠区块，优先于工作空间树。
 *
 * 存储：localStorage（与本客户端登记表 sessionWatchlist 同族——分组是本端
 * 界面策展数据，不入会话服务端 metadata，避免 PATCH 链路与多端写冲突）。
 * 有界：最多 50 组，超出丢最旧。
 * 妥协（已知边界）：会话在他端删除时本组内 sessionIds 残留悬挂 id，渲染侧
 * 按当前会话列表过滤，成员数为可见成员数；组不因此自动删除。
 */
import { uiStorage } from '@/utils/storage'

/** 一个自定义分组：组名 + 组内会话 id 集（建组时间只作排序锚） */
export interface SessionGroup {
  id: string
  name: string
  sessionIds: string[]
  createdAt: string
}

/** 分组上限：只服务手动策展面，超出按最旧淘汰 */
const MAX_GROUPS = 50

/** 组名展示上限（与标题同量级，防超长组名挤塌侧栏） */
export const GROUP_NAME_MAX = 30

function readGroups(): SessionGroup[] {
  const list = uiStorage.getSessionCustomGroups()
  if (!list) return []
  return list.filter(
    (g): g is SessionGroup =>
      !!g &&
      typeof (g as SessionGroup).id === 'string' &&
      typeof (g as SessionGroup).name === 'string' &&
      Array.isArray((g as SessionGroup).sessionIds),
  )
}

function writeGroups(groups: SessionGroup[]): void {
  uiStorage.setSessionCustomGroups(groups as unknown[])
}

function newGroupId(): string {
  return `grp-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`
}

/** 组名净化：去首尾空白、截断到上限；空串由调用方拦截（保存按钮置灰） */
export function sanitizeGroupName(name: string): string {
  return name.trim().slice(0, GROUP_NAME_MAX)
}

/** 列出全部分组（存储序，即建组序） */
export function listSessionGroups(): SessionGroup[] {
  return readGroups()
}

/** 新建空分组，返回新组（名空白抛错——调用面输入框为空时保存不可点，此为兜底） */
export function createSessionGroup(name: string): SessionGroup {
  const clean = sanitizeGroupName(name)
  if (!clean) throw new Error('分组名不能为空')
  const groups = readGroups()
  const group: SessionGroup = {
    id: newGroupId(),
    name: clean,
    sessionIds: [],
    createdAt: new Date().toISOString(),
  }
  groups.push(group)
  while (groups.length > MAX_GROUPS) groups.shift()
  writeGroups(groups)
  return group
}

/** 删除分组（成员会话不受影响，回落工作空间树/平铺列表） */
export function deleteSessionGroup(groupId: string): void {
  writeGroups(readGroups().filter((g) => g.id !== groupId))
}

/** 分配会话到组（幂等单归属：先从其他组移出再入目标组；组不存在抛错） */
export function assignSessionToGroup(groupId: string, sessionId: string): void {
  const groups = readGroups()
  for (const g of groups) {
    g.sessionIds = g.sessionIds.filter((id) => id !== sessionId)
  }
  const target = groups.find((g) => g.id === groupId)
  if (!target) throw new Error('分组不存在')
  target.sessionIds.push(sessionId)
  writeGroups(groups)
}

/** 把会话移出所有分组（不在任何组时无变化不落盘） */
export function unassignSession(sessionId: string): void {
  const groups = readGroups()
  let changed = false
  for (const g of groups) {
    const next = g.sessionIds.filter((id) => id !== sessionId)
    if (next.length !== g.sessionIds.length) {
      g.sessionIds = next
      changed = true
    }
  }
  if (changed) writeGroups(groups)
}
