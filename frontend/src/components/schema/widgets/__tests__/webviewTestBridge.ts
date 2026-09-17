/**
 * WebviewWidget 桥测试共享夹具：iframe 实例令牌读取 + postMessage 上/下行模拟。
 *
 * 各测试文件仅保留与被测语义绑定的差异（默认 id 前缀、是否 act 包裹）。
 */
import { act, screen } from '@testing-library/react'
import type { Mock } from 'vitest'

/** 从 iframe srcdoc 里读宿主注入的实例令牌（bootstrap 变量 TOKEN）。 */
export function getIframeToken(): string {
  const iframe = screen.getByTitle('Webview') as HTMLIFrameElement
  const m = (iframe.getAttribute('srcdoc') ?? '').match(/TOKEN = "([^"]+)"/)
  if (!m) throw new Error('iframe srcdoc 中未找到实例令牌 TOKEN')
  return m[1]
}

/**
 * 模拟 iframe 上行：发一条带实例令牌的合法 postMessage 事件（origin='null' + 魔数）。
 * opts.wrapAct：会触发宿主 setState 的下行处理场景需 act 包裹（hostBridge）。
 */
export function postUp(
  method: string,
  params?: unknown,
  id = 'wv_1',
  opts: { wrapAct?: boolean } = {},
): void {
  const data: Record<string, unknown> = {
    __agentos_webview: true,
    __wv_token: getIframeToken(),
    id,
    method,
  }
  if (params !== undefined) data.params = params
  const dispatch = () => window.dispatchEvent(new MessageEvent('message', { origin: 'null', data }))
  if (opts.wrapAct) act(dispatch)
  else dispatch()
}

/** 从 contentWindow.postMessage 调用记录里找指定 method 的下行消息。 */
export function findDownMessage(spy: Mock, method: string): Record<string, unknown> | undefined {
  for (const call of spy.mock.calls) {
    const msg = call[0] as Record<string, unknown> | undefined
    if (msg && typeof msg === 'object' && msg.method === method) return msg
  }
  return undefined
}

/** 全部指定 method 的下行消息（多帧断言用）。 */
export function allDownByMethod(spy: Mock, method: string): Array<Record<string, unknown>> {
  return spy.mock.calls
    .map((call) => call[0] as Record<string, unknown> | undefined)
    .filter((msg): msg is Record<string, unknown> => !!msg && msg.method === method)
}
