/**
 * postMessage 安全工具 — Webview widget 双向通信的安全边界
 *
 * VS Code Webview 模型：iframe 是独立 opaque origin（sandbox 不开 allow-same-origin），
 * 宿主与 iframe 经 postMessage 通信，必须严格校验 origin + 消息协议，防止伪造。
 *
 * 关联 ADR §3.4'（Webview widget，VS Code 风格沙箱）。
 */

/** Webview ↔ 宿主 消息协议（双向复用同一信封） */
export interface WebviewMessage {
  /** 协议魔数，校验用 */
  __agentos_webview: true
  /** 实例级令牌（安全审查 2026-08-20 B-4）：宿主注入 iframe 的一次性随机串，
   *  上行消息必须携带——origin='null' 可被任意页面伪造，魔数也不是密码学凭据，
   *  令牌才是身份。 */
  __wv_token?: string
  /** 消息 id（请求/响应配对用，事件可省略） */
  id?: string
  /** 方法名：上行如 'tool.invoke' / 'metric.record'；下行如 'widget.event' */
  method: string
  /** 参数 / 载荷 */
  params?: unknown
}

/** sandbox iframe（srcDoc + 无 allow-same-origin）的 origin 固定为 'null'（字符串）。 */
export const SANDBOX_IFRAME_ORIGIN = 'null'

/**
 * 校验 postMessage 事件的 origin 是否可信。
 *
 * srcDoc + 不开 allow-same-origin 的 iframe，其 origin 是字符串 'null'。
 * 任意网页也可发 origin='null' 的消息，所以 origin 校验只是第一道门，
 * 还需配合协议魔数（isWebviewMessage）+ 实例级令牌（validateWebviewToken）双保险。
 */
export function isTrustedWebviewOrigin(origin: string): boolean {
  return origin === SANDBOX_IFRAME_ORIGIN
}

/**
 * 校验消息是否符合 Webview 协议（含 __agentos_webview 魔数）。
 * 非 Webview 消息（如第三方脚本、浏览器扩展）会被拒绝。
 */
export function isWebviewMessage(data: unknown): data is WebviewMessage {
  if (!data || typeof data !== 'object') return false
  const msg = data as Record<string, unknown>
  return (
    msg.__agentos_webview === true &&
    typeof msg.method === 'string' &&
    msg.method.length > 0
  )
}

/**
 * 校验消息是否携带指定实例令牌。
 * 令牌在每次 WebviewWidget 挂载时由宿主生成并注入 iframe（bootstrap 变量），
 * 其它网页无法得知 → 彻底封死"任意 null-origin 页面伪造消息调用宿主 REST"面。
 */
export function validateWebviewToken(msg: WebviewMessage, expectedToken: string): boolean {
  return (
    typeof msg.__wv_token === 'string' &&
    msg.__wv_token.length > 0 &&
    expectedToken.length > 0 &&
    msg.__wv_token === expectedToken
  )
}

/**
 * 构造一条 Webview 消息（宿主 → iframe 下行用）。
 * 下行不需要令牌：我们直接向自己的 iframe contentWindow 投递，
 * 其它 frame 无从向该 iframe 注入消息。
 */
export function buildWebviewMessage(
  method: string,
  params?: unknown,
  id?: string,
): WebviewMessage {
  const msg: WebviewMessage = { __agentos_webview: true, method }
  if (params !== undefined) msg.params = params
  if (id !== undefined) msg.id = id
  return msg
}

/**
 * 完整的安全校验：origin 可信 + 消息符合协议 + 实例令牌匹配。任一失败返回 null。
 */
export function validateWebviewEvent(
  event: MessageEvent,
  expectedToken: string,
): WebviewMessage | null {
  if (!isTrustedWebviewOrigin(event.origin)) return null
  if (!isWebviewMessage(event.data)) return null
  if (!validateWebviewToken(event.data, expectedToken)) return null
  return event.data
}
