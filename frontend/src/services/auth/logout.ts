/**
 * 统一登出编排：WS/流式事件清理 + authStore.logout + SPA 跳转登录页。
 *
 * router.tsx 与 Sidebar 的登出菜单同源调用此函数，杜绝「完整清理」与
 * 「裸 logout()」两套路径并存——裸 logout 漏掉 WS 断开与流式订阅销毁，
 * 连接带着旧 token 存活到心跳死亡，登出后流式/实时事件仍向页面投递。
 *
 * navigate 由调用方注入（react-router useNavigate 产物）：service 层不持有
 * React 上下文，跳转时序统一收敛在此，调用方不再各自拼接清理步骤。
 */

import { ROUTES } from '@/constants/routes'
import { globalWS } from '@/services/websocket/GlobalWebSocket'
import { destroyStreamingEvents } from '@/services/websocket/streamingEventService'
import { useAuthStore } from '@/stores/authStore'
import { useSessionStore } from '@/stores/sessionStore'

/** 执行完整登出：清理 → 登出 → 跳转登录页。 */
export async function performLogout(navigate: (path: string) => void): Promise<void> {
  destroyStreamingEvents()
  // sessionStore 只持订阅清理与 wsStatus，连接本体的拆除在 globalWS.disconnect
  useSessionStore.getState().disconnectWebSocket()
  globalWS.disconnect()
  await useAuthStore.getState().logout()
  navigate(ROUTES.LOGIN)
}
