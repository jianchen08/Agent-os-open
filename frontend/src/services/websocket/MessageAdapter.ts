/**
 * WebSocket 消息适配器
 *
 * 仅保留 widget_event 族（P2 协议）的适配：插件向 UI 推送的 widget 交互事件。
 * 0.1 Python 内核的双向消息格式适配（adaptIncomingMessage / adaptOutgoingMessage /
 * isRustKernelMessage）已随 0.1 内核退役移除——0.2 Rust 内核消息格式不再需要适配层。
 *
 * @module MessageAdapter
 */

/** 原始 WebSocket 消息 */
export interface RawWSMessage {
  type?: string
  data?: Record<string, unknown>
  [key: string]: unknown
}

/** widget_event 适配结果（P2 协议：插件向 UI 推送的 widget 交互事件） */
export interface AdaptedWidgetEvent {
  /** 触发事件的 widget 实例 id（插件内唯一） */
  widget_id?: string
  /** 事件名（如 doc_loaded / value_changed） */
  event?: string
  /** 事件载荷 */
  data: Record<string, unknown>
  /** 全局序号（P2 传输层顺序保证用，可选） */
  sequence?: number
}

// ── widget_event 族（P2 协议）──
//
// P2 后端将下发：{ type: "widget_event", data: { widget_id, event, data }, sequence }
// 提供专用 adaptWidgetEvent 显式提取 widget_id / event / payload / sequence。

/**
 * 判断消息是否为 widget_event 族。
 *
 * @param msg - 原始消息
 * @returns 是否为 widget_event
 */
export function isWidgetEvent(msg: Record<string, unknown>): boolean {
  return msg?.type === 'widget_event'
}

/**
 * 适配 widget_event 消息，提取 widget_id / event / payload / sequence。
 *
 * @param raw - 原始 WebSocket 消息
 * @returns 适配后的 widget 事件，非 widget_event 或缺 data 包装时返回 null
 */
export function adaptWidgetEvent(raw: RawWSMessage): AdaptedWidgetEvent | null {
  if (!raw || typeof raw !== 'object') return null
  if (raw.type !== 'widget_event') return null

  const data = raw.data
  // widget_event 走 Rust 0.2 格式（必须有 data 包装层）；无 data 视为无效格式
  if (!data || typeof data !== 'object') return null
  const payload = data as Record<string, unknown>

  const inner = payload.data
  return {
    widget_id: payload.widget_id as string | undefined,
    event: payload.event as string | undefined,
    data:
      inner && typeof inner === 'object'
        ? (inner as Record<string, unknown>)
        : {},
    sequence: typeof raw.sequence === 'number' ? (raw.sequence as number) : undefined,
  }
}
