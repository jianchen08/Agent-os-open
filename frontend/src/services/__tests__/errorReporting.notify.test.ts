/** @feature FP-T12 前端适配 | @ci frontend-test */
/**
 * errorReporting 用户可见提示测试
 *
 * - reportError 统一单签名（message, options?: { type?, severity?, ...context }）
 *   （扫描批K S14：双签名内部兼容层删除，调用方可全量感知）
 * - 默认弹通知中心，显式 showToast: false 时保持静默（重试进度/401 静默等既有调用点）
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
import {
  reportError,
  captureException,
  ErrorType,
  ErrorSeverity,
} from '../errorReporting'
import * as errorReportingModule from '../errorReporting'

describe('reportError 单签名通知中心提示', () => {
  beforeEach(() => {
    addNotificationMock.mockClear()
  })

  it('默认上报时弹通知中心（标题/消息/类别）', () => {
    reportError('会话加载失败', { type: ErrorType.SERVER, severity: ErrorSeverity.ERROR })
    expect(addNotificationMock).toHaveBeenCalledTimes(1)
    const n = addNotificationMock.mock.calls[0][0]
    expect(n.category).toBe('error')
    expect(n.message).toContain('会话加载失败')
  })

  it('5xx 服务端错误优先级为 high 且 10s 自动消失（瞬时失败不常驻挂屏）', () => {
    reportError('内核不可用', { type: ErrorType.SERVER, severity: ErrorSeverity.ERROR })
    const n = addNotificationMock.mock.calls[0][0]
    expect(n.priority).toBe('high')
    expect(n.autoDismissMs).toBe(10000)
  })

  it('showToast: false 时不打扰用户（重试进度等静默调用点）', () => {
    reportError('请求失败，重试中', {
      type: ErrorType.NETWORK,
      severity: ErrorSeverity.INFO,
      showToast: false,
    })
    expect(addNotificationMock).not.toHaveBeenCalled()
  })

  it('options 缺省时仍上报（UNKNOWN 类型 / MEDIUM 级别兜底）', () => {
    reportError('未知来源的失败')
    const n = addNotificationMock.mock.calls[0][0]
    expect(n.priority).toBe('normal')
    expect(n.autoDismissMs).toBe(6000)
  })

  it('非服务端错误优先级为 normal 且自动消失', () => {
    reportError('参数校验失败', { type: ErrorType.VALIDATION, severity: ErrorSeverity.WARNING })
    const n = addNotificationMock.mock.calls[0][0]
    expect(n.priority).toBe('normal')
    expect(n.autoDismissMs).toBe(6000)
  })

  it('context.source 显式传入来源标签（统一错误模型）', () => {
    reportError('工具执行失败', { type: ErrorType.SERVER, severity: ErrorSeverity.ERROR, source: 'plugin' })
    const n = addNotificationMock.mock.calls[0][0]
    expect(n.errorSource).toBe('plugin')
  })

  it('无来源信息时 errorSource 为 undefined（渲染未知灰标）', () => {
    reportError('会话加载失败', { type: ErrorType.SERVER, severity: ErrorSeverity.ERROR })
    const n = addNotificationMock.mock.calls[0][0]
    expect(n.errorSource).toBeUndefined()
  })
})

describe('captureException 与 reportError 的分工', () => {
  beforeEach(() => {
    addNotificationMock.mockClear()
  })

  it('captureException 只落控制台不弹通知（ErrorBoundary 自带用户可见 UI）', () => {
    captureException(new Error('组件崩溃'))
    expect(addNotificationMock).not.toHaveBeenCalled()
  })
})

describe('installGlobalErrorListeners 全局异常监听（2026-08-26）', () => {
  let errorHandler: ((event: ErrorEvent) => void) | null = null
  let rejectionHandler: ((event: PromiseRejectionEvent) => void) | null = null

  beforeEach(() => {
    addNotificationMock.mockClear()
    // 捕获 addEventListener 注册的监听器（jsdom window）
    const orig = window.addEventListener.bind(window)
    window.addEventListener = vi.fn((type: string, handler: any) => {
      if (type === 'error') errorHandler = handler
      if (type === 'unhandledrejection') rejectionHandler = handler
      orig(type, handler)
    }) as any
  })

  afterEach(() => {
    ;(window.addEventListener as any).mockRestore?.()
    errorHandler = null
    rejectionHandler = null
  })

  it('window error 事件上报（通知带来源标签「前端」）', () => {
    const { installGlobalErrorListeners } = errorReportingModule
    installGlobalErrorListeners()
    errorHandler?.({ message: 'oops: 组件异步崩溃' } as ErrorEvent)
    const n = addNotificationMock.mock.calls[0][0]
    expect(n.message).toBe('oops: 组件异步崩溃')
    expect(n.errorSource).toBe('frontend')
  })

  it('unhandledrejection 以 Error 原因 message 上报', () => {
    const { installGlobalErrorListeners } = errorReportingModule
    installGlobalErrorListeners()
    rejectionHandler?.({ reason: new Error('fetch 失败') } as PromiseRejectionEvent)
    const n = addNotificationMock.mock.calls[0][0]
    expect(n.message).toBe('fetch 失败')
    expect(n.errorSource).toBe('frontend')
  })
})
