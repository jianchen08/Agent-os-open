/**
 * WebSocket 服务模块
 *
 * 统一导出所有 WebSocket 相关模块
 */

export { globalWS } from './GlobalWebSocket'
export type { ConnectionStatus } from './GlobalWebSocket'
export { WebSocketService, WebSocketStatus, webSocketService } from './WebSocketService'
export type { WebSocketStatusType } from './WebSocketService'
export type { EventHandler, EventHandlerManager } from './eventHandlers'
