/**
 * 全局错误处理和错误报告服务
 *
 * 提供统一的错误捕获、处理和报告机制
 */

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
   * 设置全局错误处理器
   */
  setupGlobalErrorHandlers(): void {
    // 捕获未处理的 JavaScript 错误
    window.addEventListener('error', (event) => {
      this.logError({
        message: event.message,
        stack: event.error?.stack,
        context: {
          filename: event.filename,
          lineno: event.lineno,
          colno: event.colno,
        },
      })
    })

    // 捕获未处理的 Promise 拒绝
    window.addEventListener('unhandledrejection', (event) => {
      this.logError({
        message: 'Unhandled Promise Rejection',
        stack: event.reason?.stack,
        context: {
          reason: event.reason?.message || String(event.reason),
        },
      })
    })

    // 开发环境下显示友好的错误提示
    if (import.meta.env.DEV) {
      console.log('[ErrorReporting] 全局错误处理器已启用')
    }
  }

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
    if (import.meta.env.DEV) {
      console.error(
        '[ErrorReporting] %s (context: %s)',
        error.message,
        error.context ? JSON.stringify(error.context) : 'none',
      )
      if (error.stack) {
        console.error('[ErrorReporting] Stack:', error.stack)
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
  }

  /**
   * 记录消息（非错误）
   */
  captureMessage(
    message: string,
    level: 'info' | 'warning' = 'info',
    context?: ErrorContext,
  ): void {
    const log = {
      message,
      context,
      timestamp: new Date(),
      userAgent: navigator.userAgent,
      url: window.location.href,
    }

    if (level === 'warning') {
      console.warn('[ErrorReporting]', log)
    } else {
      console.info('[ErrorReporting]', log)
    }
  }

  /**
   * 获取所有错误日志
   */
  getErrorLogs(): ErrorLog[] {
    return [...this.errorLogs]
  }

  /**
   * 清空错误日志
   */
  clearErrorLogs(): void {
    this.errorLogs = []
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

  /**
   * 导出错误日志为 JSON
   */
  exportErrorLogs(): string {
    return JSON.stringify(this.errorLogs, null, 2)
  }

  /**
   * 下载错误日志文件
   */
  downloadErrorLogs(): void {
    const dataStr = this.exportErrorLogs()
    const dataUri = 'data:application/json;charset=utf-8,' + encodeURIComponent(dataStr)

    const exportFileDefaultName = `error-logs-${Date.now()}.json`

    const linkElement = document.createElement('a')
    linkElement.setAttribute('href', dataUri)
    linkElement.setAttribute('download', exportFileDefaultName)
    linkElement.click()
  }
}

// 创建单例实例
const errorReportingService = new ErrorReportingService()

/**
 * 设置全局错误处理器（便捷函数）
 */
export function setupGlobalErrorHandlers(): void {
  errorReportingService.setupGlobalErrorHandlers()
}

/**
 * 捕获异常（便捷函数）
 */
export function captureException(error: Error, context?: ErrorContext): void {
  errorReportingService.captureException(error, context)
}

/**
 * 捕获消息（便捷函数）
 */
export function captureMessage(
  message: string,
  level?: 'info' | 'warning',
  context?: ErrorContext,
): void {
  errorReportingService.captureMessage(message, level, context)
}

/**
 * 报告错误（便捷函数）
 *
 * 支持两种调用方式：
 * 1. reportError(message: string, type?: ErrorType, severity?: ErrorSeverity, context?: ErrorContext)
 * 2. reportError(apiError: ApiError, context?: ErrorContext & { type?: ErrorType, severity?: ErrorSeverity })
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

/**
 * 清空错误日志（便捷函数）
 */
export function clearErrorLogs(): void {
  errorReportingService.clearErrorLogs()
}

/**
 * 下载错误日志（便捷函数）
 */
export function downloadErrorLogs(): void {
  errorReportingService.downloadErrorLogs()
}

/**
 * 导出错误日志（便捷函数）
 */
export function exportErrorLogs(): string {
  return errorReportingService.exportErrorLogs()
}

// 导出服务实例（供高级使用）
export { errorReportingService }
export type { ErrorContext, ErrorLog }
