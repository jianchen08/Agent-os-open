/**
 * message_cards 本体形态纯逻辑（ADR 2026-10-14-message-card-body-form，零依赖可单测）。
 *
 * matchBubbleCardStyleId：插件 message_cards 声明路由（宿主零样式 id/模式专名
 * 硬编码）——match.kind='bubble' 为本体形态（消息行本体交插件卡渲染），可选
 * match.modes 为会话域门（会话 modeBinding.mode ∈ modes 才生效，缺省不限）；
 * kind 缺省 = 追加形态（marker 路由，见 messageStateCard.matchStateCardStyleId，
 * 本函数不消费）。未命中/形态非法 = null（调用方回退宿主默认渲染）。
 */

/** 本体形态声明 match 形态（kind/modes 键名契约冻结于 ADR） */
interface BodyCardMatch {
  kind?: unknown
  modes?: unknown
}

/**
 * message_cards 声明 → 本体形态样式 id（kind='bubble' 且会话域门过；多声明按
 * 声明序首个命中）。mode 传活跃会话 modeBinding.mode（无绑定/非会话行传 null）。
 */
export function matchBubbleCardStyleId(
  cards: Array<Record<string, unknown>>,
  mode: string | null,
): string | null {
  for (const card of cards) {
    const match = card.match as BodyCardMatch | undefined
    if (match?.kind !== 'bubble') continue
    // 会话域门：modes 缺省/空数组 = 不限；非空数组须含活跃会话 mode
    if (Array.isArray(match.modes) && match.modes.length > 0) {
      if (!mode || !match.modes.some((m) => m === mode)) continue
    }
    const styleId = card.style_id
    if (typeof styleId === 'string' && styleId) return styleId
  }
  return null
}

/** 本体形态载荷（message.data 信封 metadata.card，ADR 冻结） */
export interface BubbleCardPayload {
  kind: 'bubble'
  role: 'user' | 'assistant'
  status: string
  agent_id?: string
  presenter?: { name: string; avatar: string | { fg?: string; bg?: string } | null } | null
}

/** 组装本体形态载荷（宿主数据路由面：role/status/agent_id 透传 + presenter 解析结果） */
export function buildBubbleCardPayload(
  role: 'user' | 'assistant',
  status: string | undefined,
  agentId: string | undefined,
  presenter: BubbleCardPayload['presenter'],
): BubbleCardPayload {
  return {
    kind: 'bubble',
    role,
    status: status ?? '',
    ...(agentId ? { agent_id: agentId } : {}),
    ...(presenter !== undefined ? { presenter } : {}),
  }
}
