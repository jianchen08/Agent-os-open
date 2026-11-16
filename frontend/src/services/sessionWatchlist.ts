/**
 * 会话登记表（watchlist）——本客户端新建会话的服务端列表窗口外补偿
 *
 * 服务端会话列表端点固定按 updated_at DESC 截断 100 条（无分页/无按 id 读取）。
 * 新建后零活动的会话（updated_at=创建时刻）会被持续活跃的其他会话挤出窗口，
 * 侧栏列表/搜索命中映射/会话树分组随之全部不可见（真机 0.2 缺陷）。
 *
 * 机制：创建成功即把会话快照登记进 localStorage；列表拉取后合并——窗口内
 * 以服务端权威值为准并回写登记快照（兜住改名等窗口内编辑），窗口外的登记
 * 会话补入列表尾部（消费方各自排序）。删除会话即注销登记。
 * 有界：最多 50 条，超出丢最旧。
 * 妥协（已知边界）：会话在他端删除时本端无事件源，登记项会残留为一个
 * 点击后加载失败的空条目，直到本端执行删除动作注销；无按 id 读取端点，
 * 无法在不发写请求的前提下做存在性复核。
 */
import { uiStorage } from '@/utils/storage'
import type { Session } from '@/types/models'

/** 登记上限：只服务"新建会话可见性"补偿面，超出按最旧淘汰 */
const MAX_WATCHLIST = 50

function readList(): Session[] {
  const list = uiStorage.getSessionWatchlist()
  return (list ?? []).filter((s): s is Session => !!s && typeof (s as Session).id === 'string')
}

function writeList(list: Session[]): void {
  uiStorage.setSessionWatchlist(list)
}

/** 登记新建会话（幂等，同 id 覆盖为新快照；超限丢最旧） */
export function rememberSession(session: Session): void {
  const list = readList().filter((s) => s.id !== session.id)
  list.push(session)
  while (list.length > MAX_WATCHLIST) {
    list.shift()
  }
  writeList(list)
}

/** 注销登记（会话删除） */
export function forgetSession(sessionId: string): void {
  const list = readList()
  if (!list.some((s) => s.id === sessionId)) return
  writeList(list.filter((s) => s.id !== sessionId))
}

/**
 * 列表拉取后合并登记会话：
 * - 窗口内命中的登记项用服务端权威快照回写（保持登记数据新鲜）；
 * - 窗口外登记项补入返回值尾部；无窗口外项且无回写变更时原样返回
 *   （保持引用稳定，不触发 query 订阅方无效重渲染）。
 */
export function mergeSessionsWithWatchlist(fetched: Session[]): Session[] {
  const list = readList()
  if (list.length === 0) return fetched

  const fetchedById = new Map(fetched.map((s) => [s.id, s]))
  let changed = false
  const kept: Session[] = list.map((entry) => {
    const fresh = fetchedById.get(entry.id)
    if (!fresh) return entry
    if (fresh !== entry) changed = true
    return fresh
  })
  if (changed) writeList(kept)

  const missing = kept.filter((s) => !fetchedById.has(s.id))
  return missing.length === 0 ? fetched : [...fetched, ...missing]
}
