/**
 * 消息状态卡纯逻辑（状态统一标记机制 §5b，2026-09-28）——零依赖可单测。
 *
 * stripBySpan：span 区间精确切除标记段本身（标记可能在正文中间，后半正文
 * 不可丢）；matchStateCardStyleId：插件 message_cards 声明路由（宿主零样式
 * id 硬编码，match.marker 命中取 style_id）。
 */

/** span 区间精确切除（标记段本身移除，前后正文保留；非法区间原样返回） */
export function stripBySpan(text: string, span: [number, number]): string {
  const [start, end] = span
  if (!text || start < 0 || end > text.length || start >= end) return text
  const merged = text.slice(0, start) + text.slice(end)
  return merged.replace(/\n{3,}/g, '\n\n').trim()
}

/** message_cards 声明 → 样式 id（marker 命中；未命中/形态非法 = null） */
export function matchStateCardStyleId(
  cards: Array<Record<string, unknown>>,
  marker: string,
): string | null {
  for (const card of cards) {
    const match = card.match as { marker?: unknown } | undefined
    const styleId = card.style_id
    if (match?.marker === marker && typeof styleId === 'string' && styleId) {
      return styleId
    }
  }
  return null
}
