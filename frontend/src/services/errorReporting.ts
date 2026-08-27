/**
 * 全局错误处理和错误报告服务
 *
 * 统一错误上报入口：
 * - DEV 下按 severity 分级打印到控制台（收敛告警噪音）
 * - 默认弹通知中心（显式 showToast:false 的调用点除外），消除
 *   「HTTP 错误只见日志不见 UI」的静默缺口
 */

import { useNotificationStore } from '../stores/notificationStore'
import type { ErrorSource } from '../types/api'

/** 上报上下文：定位信息（component/action/code/source 等）+ 控制位（showToast） */
export interface ErrorContext {
  component?: string
  action?: string
  /** 错误码（与 config/error_codes.json 对齐） */
  code?: string
  /** 错误来源标签（通知中心渲染；缺省渲染「未知」灰标，见 ErrorSourceBadge） */
  source?: ErrorSource
  /** 显式 false 时不打扰用户（重试进度、401 静默处理等既有静默调用点） */
  showToast?: boolean
  [key: string]: unknown
}

/** reportError 元信息：错误类型 + 严重级 + 定位上下文（统一单签名） */
export interface ReportOptions extends ErrorContext {
  type?: ErrorType
  severity?: ErrorSeverity
}

/**
 * 错误类型枚举
 */
export const ErrorType = {
  NETWORK: 'network',
  VALIDATION: 'validation',
  AUTHENTICATION: 'authentication',
  AUTHORIZATION: 'authorization',
  NOT_FOUND: 'not_found',
  SERVER: 'server',
  CLIENT: 'client',
  UNKNOWN: 'unknown',
} as const

export type ErrorType = (typeof ErrorType)[keyof typeof ErrorType]

/**
 * 错误严重级别枚举
 */
export const ErrorSeverity = {
  LOW: 'low',
  MEDIUM: 'medium',
  HIGH: 'high',
  CRITICAL: 'critical',
  INFO: 'info',
  WARNING: 'warning',
  ERROR: 'error',
} as const

export type ErrorSeverity = (typeof ErrorSeverity)[keyof typeof ErrorSeverity]

class ErrorReportingService {
  /**
   * 记录错误
   *
   * P4: 非业务路径 404 已降级为 WARNING severity，这里按 severity 区分打印级别
   * （WARNING/INFO → console.warn，其余 → console.error），收敛控制台告警噪音。
   * 仅在 DEV 打印——生产环境的用户可见通道是通知中心。
   */
  logError(error: { message: string; stack?: string; context?: ErrorContext }): void {
    if (!import.meta.env.DEV) return

    const severity = error.context?.errorSeverity
    const isLowSeverity = severity === 'warning' || severity === 'info'
    const logFn = isLowSeverity ? console.warn : console.error
    logFn(
      '[ErrorReporting] %s (context: %s)',
      error.message,
      error.context ? JSON.stringify(error.context) : 'none',
    )
    if (error.stack) {
      logFn('[ErrorReporting] Stack:', error.stack)
    }
  }

  /**
   * 报告错误（统一单签名：message + options 元信息）
   */
  reportError(message: string, options?: ReportOptions): void {
    const errorType = options?.type ?? ErrorType.UNKNOWN
    const errorSeverity = options?.severity ?? ErrorSeverity.MEDIUM

    // 确保类型是有效枚举值（防无效值进入下游渲染）
    const finalErrorType = Object.values(ErrorType).includes(errorType)
      ? errorType
      : ErrorType.UNKNOWN

    // type/severity 是元信息不进 context；showToast 等控制位保留在 context 内
    const { type: _type, severity: _severity, ...context } = options ?? {}

    this.logError({
      message: `[${finalErrorType.toUpperCase()}] ${message}`,
      context: {
        ...context,
        errorType: finalErrorType,
        errorSeverity,
      },
    })

    // 用户可见提示：默认开启（基础设施层统一收口）。
    // 调用方显式传 showToast: false（如重试进度、401 静默处理）时不打扰用户；
    // 其余错误一律进通知中心。
    if ((options as ErrorContext | undefined)?.showToast !== false) {
      const isServerError = finalErrorType === ErrorType.SERVER
      useNotificationStore.getState().addNotification({
        title: isServerError ? '服务请求失败' : '操作失败',
        message,
        priority: isServerError ? 'high' : 'normal',
        category: 'error',
        isBlocking: false,
        // 统一错误信封来源（config/error_codes.json）：通知中心渲染来源标签，
        // 调用方经 context.source 显式传入（client.ts 从 apiError.source 透传）。
        errorSource: context.source as ErrorSource | undefined,
        // 一律自动消失（SERVER 10s / 其余 6s）：瞬时失败（如内核重启窗口）
        // 常驻挂屏会误导用户以为服务持续不可用，且无恢复机制清除。
        autoDismissMs: isServerError ? 10000 : 6000,
      })
    }
  }
}

// 创建单例实例
const errorReportingService = new ErrorReportingService()

/**
 * 报告错误（便捷函数；统一单签名）
 */
export function reportError(message: string, options?: ReportOptions): void {
  errorReportingService.reportError(message, options)
}

/**
 * 捕获异常（便捷函数）
 *
 * 与 reportError 的差异：只落 DEV 控制台，不弹通知中心——
 * 调用方（ErrorBoundary）自身已渲染用户可见的错误 UI。
 */
export function captureException(error: Error, context?: ErrorContext): void {
  errorReportingService.logError({
    message: error.message,
    stack: error.stack,
    context,
  })
}

/**
 * 全局异常监听（统一错误模型：source=frontend）。
 * 补 ErrorBoundary 之外的异步异常盲区——组件崩溃由根 ErrorBoundary 兜底，
 * 但 window.onerror / unhandledrejection 覆盖的异步异常（事件回调/定时器/
 * 未 await 的 Promise 链）不经过组件树，此前只落 console 无用户提示。
 * 统一走 reportError：通知中心可见 + 来源标签「前端」。
 */
export function installGlobalErrorListeners(): void {
  window.addEventListener('error', (event) => {
    reportError(event.message || '前端运行时错误', {
      type: ErrorType.CLIENT,
      severity: ErrorSeverity.ERROR,
      component: 'window.onerror',
      code: 'FRONTEND_RUNTIME_ERROR',
      source: 'frontend',
    })
  })
  window.addEventListener('unhandledrejection', (event) => {
    const reason = event.reason
    const message =
      reason instanceof Error
        ? reason.message
        : typeof reason === 'string'
          ? reason
          : '未处理的 Promise 拒绝'
    reportError(message, {
      type: ErrorType.CLIENT,
      severity: ErrorSeverity.ERROR,
      component: 'unhandledrejection',
      code: 'FRONTEND_RUNTIME_ERROR',
      source: 'frontend',
    })
  })
}
