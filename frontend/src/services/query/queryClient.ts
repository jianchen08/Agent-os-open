/**
 * 全局 QueryClient 单例
 *
 * 服务端状态（会话/agent/schema/配置等 REST 镜像）统一走 TanStack Query：
 * 进入页面先渲染缓存、staleTime 到期后台静默刷新（stale-while-revalidate），
 * 组件卸载重挂不再重复请求（按 queryKey 去重）。
 *
 * 全局默认值约束：
 * - retry: false —— axios 拦截器（services/api/client.ts）已对可重试错误做
 *   2 次指数退避重试，这里再开会双重叠加退避时长。
 * - staleTime 默认 30s；变化频率低的数据（schema/agent 列表）在各 query hook
 *   里单独覆盖更长窗口。
 * - refetchOnWindowFocus: false —— 窗口 focus/visibilitychange（最大化、切全屏、
 *   alt-tab 都会触发）不该当成数据过期信号：数据新鲜度由 WS 事件推送
 *   （精准 invalidate）+ 各 query 自声明的 refetchInterval 兜底轮询承担。
 */

import { QueryClient } from '@tanstack/react-query'

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 30_000,
      gcTime: 5 * 60_000,
      retry: false,
      refetchOnWindowFocus: false,
    },
  },
})
