// @feature FP-T12 前端组件补测
/**
 * performLogout 统一登出编排测试
 *
 * 契约：清理（流式事件/会话 WS 状态/全局 WS 连接）→ authStore.logout →
 * 跳转登录页，顺序固定；router.tsx 与 Sidebar 两条登出入口同源调用。
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'

const { mockDestroyStreamingEvents } = vi.hoisted(() => ({ mockDestroyStreamingEvents: vi.fn() }))
vi.mock('@/services/websocket/streamingEventService', () => ({
  destroyStreamingEvents: mockDestroyStreamingEvents,
  initStreamingEvents: vi.fn(),
  reinitStreamingEvents: vi.fn(),
}))

import { ROUTES } from '@/constants/routes'
import { performLogout } from '../logout'
import { globalWS } from '@/services/websocket/GlobalWebSocket'
import { useAuthStore } from '@/stores/authStore'
import { useSessionStore } from '@/stores/sessionStore'

describe('performLogout 统一登出编排', () => {
  let disconnectSpy: ReturnType<typeof vi.spyOn>
  let logoutSpy: ReturnType<typeof vi.spyOn>
  let disconnectWsSpy: ReturnType<typeof vi.spyOn>
  let navigate: ReturnType<typeof vi.fn>

  beforeEach(() => {
    vi.clearAllMocks()
    disconnectSpy = vi.spyOn(globalWS, 'disconnect').mockImplementation(() => {})
    logoutSpy = vi
      .spyOn(useAuthStore.getState(), 'logout')
      .mockImplementation(async () => {})
    disconnectWsSpy = vi
      .spyOn(useSessionStore.getState(), 'disconnectWebSocket')
      .mockImplementation(() => {})
    navigate = vi.fn()
  })

  it('清理 → logout → 跳转登录页，顺序固定', async () => {
    await performLogout(navigate)

    expect(mockDestroyStreamingEvents).toHaveBeenCalledTimes(1)
    expect(disconnectWsSpy).toHaveBeenCalledTimes(1)
    expect(disconnectSpy).toHaveBeenCalledTimes(1)
    expect(logoutSpy).toHaveBeenCalledTimes(1)
    expect(navigate).toHaveBeenCalledTimes(1)
    expect(navigate).toHaveBeenCalledWith(ROUTES.LOGIN)

    // 清理步骤先于 logout，跳转在 logout 之后
    expect(mockDestroyStreamingEvents.mock.invocationCallOrder[0]).toBeLessThan(
      logoutSpy.mock.invocationCallOrder[0],
    )
    expect(disconnectSpy.mock.invocationCallOrder[0]).toBeLessThan(
      logoutSpy.mock.invocationCallOrder[0],
    )
    expect(navigate.mock.invocationCallOrder[0]).toBeGreaterThan(
      logoutSpy.mock.invocationCallOrder[0],
    )
  })

  it('logout 失败时异常向上传播，不静默吞掉', async () => {
    logoutSpy.mockRejectedValueOnce(new Error('登出接口失败'))
    await expect(performLogout(navigate)).rejects.toThrow('登出接口失败')
    // 清理已完成，但跳转不应在 logout 失败时执行
    expect(disconnectSpy).toHaveBeenCalled()
    expect(navigate).not.toHaveBeenCalled()
  })
})
