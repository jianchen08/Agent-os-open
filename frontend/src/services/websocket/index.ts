/**
 * WebSocket 服务模块
 *
 * 统一导出所有 WebSocket 相关模块
 */

export { WebSocketService, WebSocketStatus, webSocketService } from './WebSocketService'
export type { WebSocketStatusType } from './WebSocketService'
export { EnhancedMessageQueue, MessagePriority } from './EnhancedMessageQueue'
export type { MessagePriorityType } from './EnhancedMessageQueue'
export { HeartbeatManager } from './HeartbeatManager'
export type { HeartbeatCallbacks, NetworkStats } from './HeartbeatManager'
export { WebSocketErrorHandler, createWebSocketErrorHandler } from './errorHandler'
export type { ErrorHandlerResult } from './errorHandler'
export type { EventHandler, EventHandlerManager } from './eventHandlers'
export { UnifiedEventHandler } from './unifiedHandler'
export type { IUnifiedEventHandler, UnifiedIncomingMessage } from './unifiedHandler'
