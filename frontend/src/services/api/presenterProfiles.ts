/**
 * 呈现档案解析 hook（模式包插槽协议的数据层，渲染侧消费）
 *
 * 消息 agent_id（如 `mode_roleplay/card_luna`）的呈现名/头像解析：
 * - 无 `/` 或前缀不在 MODE_PRESENTER_SOURCES → 返回 null，调用方走 agents
 *   注册表老路（本协议不接管）；
 * - 命中 `mode_X/card_y` → 经插件数据端点取 {cards:[...]}，按 id===card_y
 *   匹配抽出 {name, avatar, origin}（origin=模式前缀，供渲染侧区分数据来源）；
 * - 请求失败不抛（呈现档案是装饰性数据，缺档回退 null 由调用方兜底）。
 */

import { useQuery } from '@tanstack/react-query'
import { apiClient } from '@/services/api/client'
import { queryKeys } from '@/services/query/queryKeys'
import { MODE_PRESENTER_SOURCES } from '@/services/schema/modePanel'

/** 卡目录变化频率低（改卡 yaml / 热发现才变），新鲜窗口 5 分钟 */
const PRESENTER_STALE_TIME = 5 * 60_000

/** 卡 avatar 原样透传形态（emoji 串或 {fg,bg} 色对，合法性由渲染端把关） */
export type PresenterAvatar = string | { fg?: string; bg?: string } | null

/** 模式包卡条目（/data/cards 响应子集——本协议只消费 id/name/avatar 三字段） */
export interface PresenterCard {
  id: string
  name: string
  avatar?: PresenterAvatar
}

/** 呈现档案（渲染侧按 origin 区分模式包来源与 agents 注册表来源） */
export interface PresenterProfile {
  name: string
  avatar: PresenterAvatar
  /** 数据来源模式前缀（如 mode_roleplay） */
  origin: string
}

/** `mode_X/card_y` → 前缀/卡 id；形态不符（无 `/`、空段）返回 null */
function splitModeAgentId(agentId: string): { prefix: string; cardId: string } | null {
  const slash = agentId.indexOf('/')
  if (slash <= 0 || slash === agentId.length - 1) return null
  return { prefix: agentId.slice(0, slash), cardId: agentId.slice(slash + 1) }
}

/** agentId 命中呈现数据端点；形态不符/前缀不在映射内返回 null */
export function modePresenterEndpoint(agentId: string): string | null {
  const parts = splitModeAgentId(agentId)
  if (!parts || !(parts.prefix in MODE_PRESENTER_SOURCES)) return null
  return MODE_PRESENTER_SOURCES[parts.prefix]
}

/**
 * 从卡目录解析呈现档案：agentId 形态/前缀映射/id 匹配三关皆过才产出，
 * 任一不过返回 null（纯函数，便于单测前缀解析/id 匹配/未命中三态）。
 */
export function resolvePresenterFromCards(
  agentId: string,
  cards: PresenterCard[],
): PresenterProfile | null {
  const parts = splitModeAgentId(agentId)
  if (!parts || !(parts.prefix in MODE_PRESENTER_SOURCES)) return null
  const card = cards.find((c) => c.id === parts.cardId)
  if (!card) return null
  return { name: card.name, avatar: card.avatar ?? null, origin: parts.prefix }
}

/** 拉卡目录并解析（gate 不过零请求直接 null，与 hook 的 enabled 同口径） */
async function fetchPresenterProfile(agentId: string): Promise<PresenterProfile | null> {
  const endpoint = modePresenterEndpoint(agentId)
  if (!endpoint) return null
  const res = await apiClient.get<{ cards?: PresenterCard[] }>(endpoint)
  return resolvePresenterFromCards(agentId, res.data.cards ?? [])
}

/**
 * usePresenterProfile — agent_id 的呈现档案（{name, avatar, origin} | null）。
 * null 语义双关：非模式包 agent（走 agents 注册表老路）或模式包暂查不到档
 * （请求失败/卡不存在），调用方统一回退注册表/agent_id 兜底展示。
 */
export function usePresenterProfile(agentId: string | undefined): PresenterProfile | null {
  const resolvable = !!agentId && modePresenterEndpoint(agentId) !== null
  const query = useQuery({
    queryKey: queryKeys.presenterProfile(agentId ?? ''),
    queryFn: () => fetchPresenterProfile(agentId ?? ''),
    enabled: resolvable,
    staleTime: PRESENTER_STALE_TIME,
    // 装饰性数据失败即回退 null，不重试轰打插件端点
    retry: false,
  })
  if (!resolvable || !agentId) return null
  return query.data ?? null
}
