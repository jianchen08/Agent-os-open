/**
 * 消息状态卡分发 hook（状态统一标记机制 §5b，2026-09-28）
 *
 * 「看情况」三道门（缺一即 null，零渲染语义）：
 * 1. active：仅最新 assistant 消息且非流式（历史/流式中不挂）；
 * 2. 载荷：活跃会话主管道的解析步端点载荷在场（GET /ext/state_marker_parse/
 *    latest?pipeline_id=——pipelineStates 是内核白名单裁剪视图不透出该键，
 *    插件端点拉取是零内核边界下的唯一通道）；
 * 3. 声明：插件 message_cards 声明命中（match.marker='state' → style_id），
 *    registry 无声明 = 同源消失（宿主零样式 id 硬编码）。
 *
 * 正文重组按 span 精确切除标记段本身（前段+后段拼接——标记可能在正文中间，
 * 后半正文不可丢）。
 */
import { useEffect, useState } from 'react'
import { PIPELINE_STATE_MARKER_PARSE_ENDPOINTS } from '@/services/api/endpoints.generated'
import { queryClient } from '@/services/query/queryClient'
import { apiClient } from '@/services/api/client'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import { resolveMessageStyle } from '@/components/chat/PluginMessageCard'
import { readSessions } from '@/hooks/queries/useSessionsQuery'
import { useSessionStore } from '@/stores/sessionStore'
import { mainPipelineIdOf } from '@/utils/mappers'

/** 解析步端点回执（state_updates 载荷） */
interface StateUpdatesPayload {
  entries: Record<string, unknown>
  span: [number, number]
  ts: string
}

/** 分发结果（调用方：正文按 span 精确切除 + 尾部 PluginMessageCard） */
export interface MessageStateCard {
  styleId: string
  entries: Record<string, unknown>
  /** 标记段在原文的区间（调用方对本消息 content 做精确切除） */
  span: [number, number]
}

export { stripBySpan, matchStateCardStyleId } from '@/services/schema/messageStateCard'
import { matchStateCardStyleId as _matchStyleId } from '@/services/schema/messageStateCard'

async function fetchLatestPayload(pipelineId: string): Promise<StateUpdatesPayload | null> {
  const res = await apiClient.get<{ state_updates: StateUpdatesPayload | null }>(
    `${PIPELINE_STATE_MARKER_PARSE_ENDPOINTS.state_marker_latest}?pipeline_id=${encodeURIComponent(pipelineId)}`,
  )
  return res.data.state_updates ?? null
}

/** 活跃会话主管道的最新状态卡载荷（active=false 零请求；模块级 queryClient
 *  fetchQuery——不用 useQuery，MessageItem 无条件路径不引入 Provider 依赖） */
export function useMessageStateCard(active: boolean): MessageStateCard | null {
  const activeSessionId = useSessionStore((s) => s.activeSessionId)
  const session = readSessions().find((s) => s.id === activeSessionId)
  const pipelineId = session ? mainPipelineIdOf(session) : undefined
  const [card, setCard] = useState<MessageStateCard | null>(null)

  useEffect(() => {
    if (!active || !pipelineId) {
      setCard(null)
      return
    }
    let cancelled = false
    queryClient
      .fetchQuery({
        queryKey: ['state-marker-latest', pipelineId],
        queryFn: () => fetchLatestPayload(pipelineId),
        staleTime: 15_000,
      })
      .then((payload) => {
        if (cancelled || !payload) {
          setCard(null)
          return
        }
        const styleId = _matchStyleId(contributionRegistry.getAllMessageCards(), 'state')
        if (!styleId || !resolveMessageStyle(styleId)) {
          setCard(null)
          return
        }
        setCard({ styleId, entries: payload.entries, span: payload.span })
      })
      .catch(() => setCard(null))
    return () => {
      cancelled = true
    }
  }, [active, pipelineId])

  return active ? card : null
}
