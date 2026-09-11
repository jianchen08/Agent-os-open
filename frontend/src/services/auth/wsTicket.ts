/**
 * WS 一次性握手票据获取（内核契约，kernel/crates/api/src/ws_ticket.rs）。
 *
 * POST /api/v1/ws-ticket（Authorization Bearer 由 apiClient 统一注入）→
 * 200 {"ticket": string, "expires_in": 60}。ticket 用于 /ws/chat?ticket=
 * 握手：单次消费、60s TTL，无效/复用/过期一律 4001；?token= 路径并存保留
 * （测试/外部脚本），前端生产路径一律先取票再建连，每次连接独立取票。
 */

import apiClient from '@/services/api/client'

/** 内核 WS 票据签发端点（POST，任意已认证用户可签发） */
const WS_TICKET_ENDPOINT = '/api/v1/ws-ticket'

/** 签发一次性 WS 握手票据；网络/认证失败经 apiClient 以错误抛出，由调用方走重连退避 */
export async function fetchWsTicket(): Promise<string> {
  const response = await apiClient.post<{ ticket?: string }>(WS_TICKET_ENDPOINT)
  const ticket = response.data?.ticket
  if (!ticket) {
    throw new Error('WS 票据响应缺少 ticket 字段')
  }
  return ticket
}
