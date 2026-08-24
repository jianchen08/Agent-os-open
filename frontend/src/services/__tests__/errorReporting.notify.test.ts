/** @feature FP-T12 前端适配 | @ci frontend-test */
/**
 * errorReporting 用户可见提示测试（2026-08-22 错误透传收口）
 *
 * reportError 此前只写内存日志（用户无感知）；本次默认弹通知中心，
 * 显式 showToast: false 时保持静默（重试进度/401 静默等既有调用点）。
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'

const { addNotificationMock } = vi.hoisted(() => ({
  addNotificationMock: vi.fn(),
}))

// mock notificationStore：捕获 addNotification 调用
vi.mock('../../stores/notificationStore', () => ({
  useNotificationStore: {
    getState: () => ({ addNotification: addNotificationMock }),
  },
}))

// 重新导入模块（mock 提升生效）
import { reportError, ErrorType, ErrorSeverity, getErrorLogs } from '../errorReporting'

describe('errorReporting 通知中心提示（2026-08-22）', () => {
  beforeEach(() => {
    addNotificationMock.mockClear()
  })

  it('默认上报时弹通知中心（标题/消息/类别）', () => {
    reportError('会话加载失败', ErrorType.SERVER, ErrorSeverity.ERROR)
    expect(addNotificationMock).toHaveBeenCalledTimes(1)
    const n = addNotificationMock.mock.calls[0][0]
    expect(n.category).toBe('error')
    expect(n.message).toContain('会话加载失败')
  })

  it('5xx 服务端错误优先级为 high 且 10s 自动消失（瞬时失败不常驻挂屏）', () => {
    reportError('内核不可用', ErrorType.SERVER, ErrorSeverity.ERROR)
    const n = addNotificationMock.mock.calls[0][0]
    expect(n.priority).toBe('high')
    expect(n.autoDismissMs).toBe(10000)
  })

  it('showToast: false 时不打扰用户（重试进度等静默调用点）', () => {
    reportError('请求失败，重试中', ErrorType.NETWORK, ErrorSeverity.INFO, {
      showToast: false,
    })
    expect(addNotificationMock).not.toHaveBeenCalled()
    // 日志仍应记录（排查链路不丢）
    expect(getErrorLogs().length).toBeGreaterThan(0)
  })

  it('非服务端错误优先级为 normal 且自动消失', () => {
    reportError('参数校验失败', ErrorType.VALIDATION, ErrorSeverity.WARNING)
    const n = addNotificationMock.mock.calls[0][0]
    expect(n.priority).toBe('normal')
    expect(n.autoDismissMs).toBe(6000)
  })
})
