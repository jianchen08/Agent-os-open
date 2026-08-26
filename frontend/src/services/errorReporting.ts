/**
 * 全局错误处理和错误报告服务
 *
 * 提供统一的错误捕获、处理和报告机制
 */

import { useNotificationStore } from '../stores/notificationStore'
import type { ApiError } from '../types/api'

interface ErrorContext {
  component?: string
  action?: string
  [key: string]: any
}

interface ErrorLog {
  message: string
  stack?: string
  context?: ErrorContext
  timestamp: Date
  userAgent: string
  url: string
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
  private errorLogs: ErrorLog[] = []
  private maxLogs = 50 // 最多保存50条错误日志

  /**
   * 记录错误
   */
  logError(error: { message: string; stack?: string; context?: ErrorContext }): void {
    const errorLog: ErrorLog = {
      ...error,
      timestamp: new Date(),
      userAgent: navigator.userAgent,
      url: window.location.href,
    }

    // 添加到日志队列
    this.errorLogs.push(errorLog)

    // 限制日志数量
    if (this.errorLogs.length > this.maxLogs) {
      this.errorLogs.shift()
    }

    // 开发环境下打印详细信息
    // P4: 非业务路径 404 已降级为 WARNING severity，这里按 severity 区分打印级别
    // （WARNING/INFO → console.warn，ERROR → console.error），收敛控制台告警噪音
    if (import.meta.env.DEV) {
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

    // 生产环境可以发送到错误跟踪服务
    if (!import.meta.env.DEV) {
      this.sendToErrorTracking(errorLog)
    }
  }

  /**
   * 记录错误（便捷方法）
   */
  captureException(error: Error, context?: ErrorContext): void {
    this.logError({
      message: error.message,
      stack: error.stack,
      context,
    })
  }

  /**
   * 报告错误（供API客户端使用）
   *
   * 支持两种调用方式：
   * 1. reportError(message: string, type?: ErrorType, severity?: ErrorSeverity, context?: ErrorContext)
   * 2. reportError(apiError: ApiError, context?: ErrorContext & { type?: ErrorType, severity?: ErrorSeverity })
   */
  reportError(
    message: string | ApiError,
    typeOrContext?: ErrorType | (ErrorContext & { type?: ErrorType; severity?: ErrorSeverity }),
    severity?: ErrorSeverity,
    context?: ErrorContext,
  ): void {
    let errorMessage: string
    let errorType: ErrorType = ErrorType.UNKNOWN
    let errorSeverity: ErrorSeverity = ErrorSeverity.MEDIUM
    let errorContext: ErrorContext = {}

    if (typeof message === 'string') {
      // 调用方式 1: reportError(message, type, severity, context)
      errorMessage = message
      errorType = (typeOrContext as ErrorType) || ErrorType.UNKNOWN
      errorSeverity = severity || ErrorSeverity.MEDIUM
      errorContext = context || {}
    } else {
      // 调用方式 2: reportError(apiError, context)
      errorMessage = message.message || message.code || '未知错误'
      const ctx =
        (typeOrContext as ErrorContext & {
          type?: ErrorType
          severity?: ErrorSeverity
        }) || {}
      errorType = ctx.type || ErrorType.UNKNOWN
      errorSeverity = ctx.severity || ErrorSeverity.MEDIUM
      errorContext = { ...ctx }
      // 移除 type 和 severity，避免重复
      delete errorContext.type
      delete errorContext.severity
    }

    // 确保 errorType 是有效的枚举值（防止传入无效值）
    const validErrorTypes = Object.values(ErrorType)
    const finalErrorType = validErrorTypes.includes(errorType) ? errorType : ErrorType.UNKNOWN

    this.logError({
      message: `[${finalErrorType.toUpperCase()}] ${errorMessage}`,
      context: {
        ...errorContext,
        errorType: finalErrorType,
        errorSeverity,
      },
    })

    // 用户可见提示：默认开启（基础设施层统一收口）。
    // 调用方显式传 showToast: false（如重试进度、401 静默处理）时不打扰用户；
    // 其余错误一律进通知中心，消除"HTTP 错误只见日志不见 UI"的静默缺口。
    if (errorContext.showToast !== false) {
      const isServerError = finalErrorType === ErrorType.SERVER
      useNotificationStore.getState().addNotification({
        title: isServerError ? '服务请求失败' : '操作失败',
        message: errorMessage,
        priority: isServerError ? 'high' : 'normal',
        category: 'error',
        isBlocking: false,
        // 统一错误信封来源（config/error_codes.json）：通知中心渲染来源标签。
        // 取数路径：reportError(apiError) 直接取信封 source；字符串调用方经
        // context.source 显式传入。
        errorSource: typeof message === 'object' ? message.source : errorContext.source,
        // 一律自动消失（SERVER 10s / 其余 6s）：瞬时失败（如内核重启窗口）
        // 常驻挂屏会误导用户以为服务持续不可用，且无恢复机制清除。
        autoDismissMs: isServerError ? 10000 : 6000,
      })
    }
  }

  /**
   * 获取所有错误日志
   */
  getErrorLogs(): ErrorLog[] {
    return [...this.errorLogs]
  }

  /**
   * 发送错误到错误跟踪服务（占位符）
   */
  private sendToErrorTracking(errorLog: ErrorLog): void {
    // TODO: 实现发送到错误跟踪服务（如 Sentry）
    // 这里可以添加 API 调用或其他错误跟踪服务
    if (import.meta.env.DEV) {
      console.log('[ErrorReporting] 错误已记录，生产环境将发送到跟踪服务', errorLog)
    }
  }
}

// 创建单例实例
const errorReportingService = new ErrorReportingService()

/**
 * 捕获异常（便捷函数）
 */
export function captureException(error: Error, context?: ErrorContext): void {
  errorReportingService.captureException(error, context)
}

/**
 * 报告错误（便捷函数）
 *
 * 支持两种调用方式：
 * 1. reportError(message: string, type?: ErrorType, severity?: ErrorSeverity, context?: ErrorContext)
 * 2. reportError(apiError: ApiError, context?: ErrorContext & { type?: ErrorType; severity?: ErrorSeverity })
 */
export function reportError(
  message: string | ApiError,
  typeOrContext?: ErrorType | (ErrorContext & { type?: ErrorType; severity?: ErrorSeverity }),
  severity?: ErrorSeverity,
  context?: ErrorContext,
): void {
  errorReportingService.reportError(message, typeOrContext, severity, context)
}

/**
 * 获取错误日志（便捷函数）
 */
export function getErrorLogs(): ErrorLog[] {
  return errorReportingService.getErrorLogs()
}

export type { ErrorContext, ErrorLog }
