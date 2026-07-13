/**
 * WebSocket 服务门面
 *
 * 统一导出全局 WebSocket 连接实例和类型，供外部模块引用。
 * 实际实现在 @/services/websocket/GlobalWebSocket.ts。
 *
 * 公共接口：
 * - globalWS — 全局 WebSocket 单例（subscribe / unsubscribe / send*）
 * - ConnectionStatus — 连接状态类型
 */

export { globalWS } from './websocket/GlobalWebSocket'
export type { ConnectionStatus } from './websocket/GlobalWebSocket'
