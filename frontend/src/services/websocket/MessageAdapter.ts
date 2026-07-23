/**
 * WebSocket 消息适配器
 *
 * 适配 Rust 内核（0.2）与 Python 内核（0.1）的 WebSocket 消息格式差异。
 *
 * 消息格式：
 * - Rust 内核（0.2）：{ type: string, data: {...}, metadata?: {...} }
 * - Python 内核（0.1）：{ type: string, thread_id: string, content: string, ... }
 *
 * 适配策略：双向兼容——两种格式都能被前端正确解析。
 *
 * @module MessageAdapter
 */

/** 原始 WebSocket 消息（两种格式可能的字段并集） */
export interface RawWSMessage {
  type?: string
  data?: Record<string, unknown>
  metadata?: Record<string, unknown>
  thread_id?: string
  content?: string
  pipeline_id?: string
  [key: string]: unknown
}

/** 适配后的标准前端消息格式 */
export interface AdaptedWSMessage {
  /** 消息类型 */
  type: string
  /** 消息数据（统一包装在 data 中） */
  data: Record<string, unknown>
  /** 会话/线程 ID（从顶层或 data 中提取） */
  thread_id?: string
  /** 管道 ID（从顶层或 data 中提取） */
  pipeline_id?: string
  /** 元数据（可选） */
  metadata?: Record<string, unknown>
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

/**
 * 判断消息是否为 Rust 内核格式（含 data 包装层）
 *
 * @param msg - 原始消息
 * @returns 是否为 Rust 内核格式
 */
export function isRustKernelMessage(msg: Record<string, unknown>): boolean {
  return 'data' in msg && typeof msg.data === 'object' && msg.data !== null
}

/**
 * 适配入站消息（Rust 内核 / Python 内核 → 前端标准格式）
 *
 * 处理两种格式：
 * 1. Rust 格式：{ type, data: { thread_id, content, ... }, metadata? }
 * 2. Python 格式：{ type, thread_id, content, pipeline_id, ... }（扁平结构）
 *
 * @param raw - 原始 WebSocket 消息
 * @returns 适配后的标准消息，无效消息返回 null
 */
export function adaptIncomingMessage(raw: RawWSMessage): AdaptedWSMessage | null {
  if (!raw || typeof raw !== 'object') return null
  if (!raw.type || typeof raw.type !== 'string') return null

  // Rust 格式：data 包装层存在
  if (raw.data && typeof raw.data === 'object') {
    const data = raw.data as Record<string, unknown>
    return {
      type: raw.type,
      data,
      thread_id: data.thread_id as string | undefined,
      pipeline_id: data.pipeline_id as string | undefined,
      metadata: raw.metadata,
    }
  }

  // Python 格式：扁平结构，提取已知字段到 data
  const { type, metadata, ...rest } = raw
  const data: Record<string, unknown> = { ...rest }
  return {
    type,
    data,
    thread_id: rest.thread_id as string | undefined,
    pipeline_id: rest.pipeline_id as string | undefined,
    metadata,
  }
}

/**
 * 适配出站消息（前端 → Rust 内核格式）
 *
 * 将前端的扁平消息结构包装为 Rust 内核期望的 { type, data, metadata } 格式。
 * 向后兼容：如果消息已经是 { type, data } 结构，则直接透传。
 *
 * @param msg - 前端待发送的消息
 * @returns 适配为 Rust 内核格式的消息
 */
export function adaptOutgoingMessage(msg: Record<string, unknown>): {
  type: string
  data: Record<string, unknown>
  metadata?: Record<string, unknown>
} {
  const { type, metadata, ...rest } = msg

  // 已经是 { type, data } 结构，直接透传
  if ('data' in rest && typeof rest.data === 'object') {
    return {
      type: type as string,
      data: rest.data as Record<string, unknown>,
      metadata: metadata as Record<string, unknown> | undefined,
    }
  }

  // 扁平结构 → 包装到 data 中
  return {
    type: type as string,
    data: rest,
    metadata: metadata as Record<string, unknown> | undefined,
  }
}

// ── widget_event 族（P2 协议）──
//
// P2 后端将下发：{ type: "widget_event", data: { widget_id, event, data }, sequence }
// data 包装层是 Rust 0.2 格式，但顶层 sequence 字段在通用 adaptIncomingMessage
// 中会被丢弃（Rust 分支只保留 data/metadata），故提供专用 adaptWidgetEvent
// 显式提取 widget_id / event / payload / sequence。

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
