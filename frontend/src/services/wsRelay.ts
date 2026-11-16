/**
 * 远端内核实时通道中继（桌面版，2026-10-22）。
 *
 * 背景：打包件渲染页 origin=app://（安全源），安全源发 ws:// 远端属混合内容
 * → 渲染层 WebSocket 握手被浏览器拦（fetch/XHR 有 allow-running-insecure-content
 * 放行，WS 不在其列——CDP 实测：到远端 ws:// 6 秒不 open 不 error）。主进程起
 * 回环 WS 端点代转接（electron/ws-relay.ts，内核零改动），本模块只负责：
 * 声明当前内核基址 + 取中继 URL。
 *
 * 无 electronAPI（浏览器/dev/移动壳）→ 返回 null，调用方回落直连（移动壳
 * origin=http://localhost 非安全源，无混合内容约束，直连本就可用）。
 */
import { PROTOCOL_VERSION } from '@/constants/websocket'

interface WsRelayApi {
  allow(target: string | null): Promise<boolean>
  url(params: {
    target: string
    ticket: string
    version?: string
    last_sequence?: number
  }): Promise<string | null>
}

function relayApi(): WsRelayApi | null {
  const api = (
    window as unknown as {
      electronAPI?: { wsRelay?: WsRelayApi }
    }
  ).electronAPI?.wsRelay
  return api ?? null
}

/**
 * 取中继 WS URL：声明目标基址（主进程只转发到它，非开放代理）后取 URL。
 * 目标未授权/参数非法/无宿主能力 → null（调用方回落直连）。
 */
export async function resolveRelayWsUrl(
  base: string,
  ticket: string,
  lastSequence?: number,
): Promise<string | null> {
  if (!base) return null
  const api = relayApi()
  if (!api) return null
  try {
    await api.allow(base)
    return await api.url({
      target: base,
      ticket,
      version: PROTOCOL_VERSION,
      ...(lastSequence != null && lastSequence > 0
        ? { last_sequence: lastSequence }
        : {}),
    })
  } catch {
    // 中继不可用（主进程未起/未授权）→ 直连兜底
    return null
  }
}
