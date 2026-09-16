/**
 * GlobalWebSocket 测试家族共享的 MockWebSocket 脚手架。
 *
 * 类型/类/实例表曾在 test 与 gaps 两个测试文件逐字复制（jscpd 克隆门禁重复源）。
 * 语义为两份拷贝的超集：bufferedAmount/url 字段取自 gaps 版，close 行为两版一致。
 */
import { vi } from 'vitest'

export type MockEventListener = ((event: any) => void) | null

export interface MockWebSocketInstance {
  onopen: MockEventListener
  onclose: MockEventListener
  onmessage: MockEventListener
  onerror: MockEventListener
  send: ReturnType<typeof vi.fn>
  close: ReturnType<typeof vi.fn>
  bufferedAmount: number
  readyState: number
  url: string
}

export const instances: MockWebSocketInstance[] = []

export class MockWebSocket {
  static OPEN = 1
  static CLOSED = 3
  static CONNECTING = 0

  onopen: MockEventListener = null
  onclose: MockEventListener = null
  onmessage: MockEventListener = null
  onerror: MockEventListener = null
  send = vi.fn()
  bufferedAmount = 0
  readyState = MockWebSocket.CONNECTING

  constructor(public url: string) {
    instances.push(this as unknown as MockWebSocketInstance)
  }

  close = vi.fn((code?: number, reason?: string) => {
    this.readyState = MockWebSocket.CLOSED
    if (this.onclose) {
      this.onclose({ code: code ?? 1000, reason: reason ?? '' })
    }
  })
}

/** createService 公共尾段：装配全局 WS、清空实例表、动态导入被测模块并返回操作句柄 */
export async function finishWsServiceSetup() {
  vi.stubGlobal('WebSocket', MockWebSocket)
  instances.length = 0
  const mod = await import('../../GlobalWebSocket')
  const service = mod.globalWS
  return {
    service,
    connect: (token: string) => service.connect(token),
    disconnect: () => service.disconnect(),
    getLatestWs: () => instances[instances.length - 1],
  }
}

/** logger.websocket 四件套 mock（与各文件内联 vi.mock 同形） */
export function loggerWebsocketMock() {
  return {
    loggers: {
      websocket: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
    },
  }
}
