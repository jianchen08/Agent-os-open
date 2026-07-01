/**
 * 常量统一导出
 */

// 导出路由常量
export { ROUTES } from './routes'
export type { RoutePath } from './routes'

// 导出API常量
export { API_BASE_URL, API_ENDPOINTS, API_TIMEOUT, API_RETRY_COUNT, API_RETRY_DELAY } from './api'

// 导出WebSocket常量
export {
  WS_BASE_URL,
  WS_HEARTBEAT_CONFIG,
  WS_SERVER_EVENTS,
  WS_CLIENT_MESSAGES,
  APPROVAL_DECISIONS,
} from './websocket'
export type {
  WebSocketServerEventType,
  WebSocketClientMessageType,
  ApprovalDecisionType,
} from './websocket'
