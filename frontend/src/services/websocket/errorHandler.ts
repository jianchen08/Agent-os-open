/**
 * WebSocket 统一错误处理模块
 *
 * 提供错误分类、重试策略和用户提示功能
 */

import { DEFAULT_RETRY_POLICY, WebSocketErrorCode } from '@/constants/websocket'
import type { RetryPolicy } from '@/constants/websocket'

/**
 * 错误信息接口
 */
export interface ErrorInfo {
  /** 错误码 */
  code: WebSocketErrorCode
  /** 错误消息 */
  message: string
  /** 原始错误对象 */
  originalError?: Error | Event
  /** 错误发生时间戳 */
  timestamp: number
  /** 附加上下文信息 */
  context?: Record<string, unknown>
}

/**
 * 错误处理结果
 */
export interface ErrorHandlerResult {
  /** 是否应该重试 */
  shouldRetry: boolean
  /** 重试延迟时间（毫秒） */
  retryDelay: number
  /** 用户可见的错误提示 */
  userMessage: string
  /** 是否需要用户手动操作 */
  requiresUserAction: boolean
  /** 建议的操作 */
  suggestedAction?: string
}

/**
 * 错误上报器接口
 */
export interface ErrorReporter {
  /** 上报错误信息 */
  reportError(errorInfo: ErrorInfo): Promise<void>
  /** 上报指标数据 */
  reportMetric(name: string, value: number, tags?: Record<string, string>): Promise<void>
}

/**
 * 控制台错误上报器（默认实现）
 */
class ConsoleErrorReporter implements ErrorReporter {
  /**
   * 上报错误信息到控制台
   *
   * @param errorInfo 错误信息
   */
  async reportError(errorInfo: ErrorInfo): Promise<void> {
    console.error('[WebSocket错误上报]', errorInfo)
  }

  /**
   * 上报指标数据到控制台
   *
   * @param name 指标名称
   * @param value 指标值
   * @param tags 标签
   */
  async reportMetric(name: string, value: number, tags?: Record<string, string>): Promise<void> {
    console.log('[WebSocket指标上报]', { name, value, tags })
  }
}

/**
 * WebSocket错误处理器
 *
 * 负责错误分类、重试决策和用户提示
 */
export class WebSocketErrorHandler {
  /** 重试策略 */
  private retryPolicy: RetryPolicy

  /** 错误上报器 */
  private errorReporter: ErrorReporter

  /**
   * 创建WebSocket错误处理器
   *
   * @param retryPolicy 重试策略
   * @param errorReporter 错误上报器
   */
  constructor(retryPolicy: RetryPolicy = DEFAULT_RETRY_POLICY, errorReporter?: ErrorReporter) {
    this.retryPolicy = retryPolicy
    this.errorReporter = errorReporter || new ConsoleErrorReporter()
  }

  /**
   * 处理错误
   *
   * @param error 原始错误对象
   * @param context 附加上下文
   * @param retryCount 当前重试次数
   * @returns 错误处理结果
   */
  async handleError(
    error: Error | Event | CloseEvent,
    context?: Record<string, unknown>,
    retryCount: number = 0,
  ): Promise<ErrorHandlerResult> {
    const errorInfo = this.classifyError(error, context)

    await this.errorReporter.reportError(errorInfo)

    const shouldRetry = this.shouldRetry(errorInfo.code, retryCount)
    const retryDelay = shouldRetry ? this.calculateRetryDelay(retryCount) : 0
    const userMessage = this.getUserMessage(errorInfo.code)
    const requiresUserAction = this.requiresUserAction(errorInfo.code)
    const suggestedAction = this.getSuggestedAction(errorInfo.code)

    return {
      shouldRetry,
      retryDelay,
      userMessage,
      requiresUserAction,
      suggestedAction,
    }
  }

  /**
   * 分类错误
   *
   * 根据错误类型将其映射为对应的WebSocket错误码
   *
   * @param error 原始错误对象
   * @param context 附加上下文
   * @returns 分类后的错误信息
   */
  private classifyError(
    error: Error | Event | CloseEvent,
    context?: Record<string, unknown>,
  ): ErrorInfo {
    let code: WebSocketErrorCode
    let message: string

    if (error instanceof CloseEvent) {
      switch (error.code) {
        case 4001:
          code = WebSocketErrorCode.AUTH_FAILED
          message = '认证失败'
          break
        case 4002:
          code = WebSocketErrorCode.TOKEN_EXPIRED
          message = '令牌已过期'
          break
        case 4003:
          code = WebSocketErrorCode.CONNECTION_LIMIT
          message = '连接数超限'
          break
        case 1006:
          code = WebSocketErrorCode.CONNECTION_LOST
          message = '连接异常断开'
          break
        case 4004:
          // 后端用 4004 表示连接被新连接替换
          // 不应触发自动重连，否则会形成断连-重连死循环
          code = WebSocketErrorCode.CONNECTION_REPLACED
          message = '连接已被新连接替换'
          break
        default:
          code = WebSocketErrorCode.CONNECTION_LOST
          message = `连接关闭: ${error.reason || '未知原因'}`
      }
    } else if (error instanceof Error) {
      if (error.name === 'NetworkError' || error.message.includes('network')) {
        code = WebSocketErrorCode.UNREACHABLE
        message = '网络不可达'
      } else if (error.message.includes('timeout')) {
        code = WebSocketErrorCode.TIMEOUT
        message = '连接超时'
      } else {
        code = WebSocketErrorCode.SERVER_ERROR
        message = error.message || '服务器内部错误'
      }
    } else {
      code = WebSocketErrorCode.SERVER_ERROR
      message = '未知错误'
    }

    return {
      code,
      message,
      originalError: error,
      timestamp: Date.now(),
      context,
    }
  }

  /**
   * 判断是否应该重试
   *
   * @param code 错误码
   * @param retryCount 当前重试次数
   * @returns 是否应该重试
   */
  private shouldRetry(code: WebSocketErrorCode, retryCount: number): boolean {
    if (retryCount >= this.retryPolicy.maxRetries) {
      return false
    }
    return this.retryPolicy.retryableErrors.has(code)
  }

  /**
   * 计算重试延迟时间（指数退避 + 抖动）
   *
   * @param retryCount 当前重试次数
   * @returns 延迟时间（毫秒）
   */
  private calculateRetryDelay(retryCount: number): number {
    const delay = Math.min(
      this.retryPolicy.initialDelay * Math.pow(this.retryPolicy.backoffFactor, retryCount),
      this.retryPolicy.maxDelay,
    )

    const jitter = delay * 0.1 * Math.random()
    return Math.floor(delay + jitter)
  }

  /**
   * 获取用户可见的错误提示
   *
   * @param code 错误码
   * @returns 用户可见的错误提示文本
   */
  private getUserMessage(code: WebSocketErrorCode): string {
    const messages: Record<WebSocketErrorCode, string> = {
      [WebSocketErrorCode.AUTH_FAILED]: '认证失败，请重新登录',
      [WebSocketErrorCode.TOKEN_EXPIRED]: '登录已过期，正在自动刷新...',
      [WebSocketErrorCode.CONNECTION_LIMIT]: '连接数过多，请稍后重试',
      [WebSocketErrorCode.CONNECTION_LOST]: '连接中断，正在重连...',
      [WebSocketErrorCode.TIMEOUT]: '连接超时，正在重试...',
      [WebSocketErrorCode.UNREACHABLE]: '网络不可达，请检查网络连接',
      [WebSocketErrorCode.SERVER_ERROR]: '服务器暂时不可用，正在重试...',
      [WebSocketErrorCode.RATE_LIMITED]: '请求过于频繁，请稍后重试',
      [WebSocketErrorCode.MAINTENANCE]: '系统维护中，请稍后重试',
      [WebSocketErrorCode.MESSAGE_TOO_LARGE]: '消息内容过大，请减少内容后重试',
      [WebSocketErrorCode.INVALID_FORMAT]: '消息格式错误，请重试',
      [WebSocketErrorCode.UNSUPPORTED_TYPE]: '不支持的消息类型',
      [WebSocketErrorCode.CONNECTION_REPLACED]: '连接已被新连接替换',
    }

    return messages[code] || '发生未知错误，请重试'
  }

  /**
   * 判断是否需要用户手动操作
   *
   * @param code 错误码
   * @returns 是否需要用户手动操作
   */
  private requiresUserAction(code: WebSocketErrorCode): boolean {
    const userActionRequired = new Set([
      WebSocketErrorCode.AUTH_FAILED,
      WebSocketErrorCode.CONNECTION_LIMIT,
      WebSocketErrorCode.MESSAGE_TOO_LARGE,
      WebSocketErrorCode.INVALID_FORMAT,
      WebSocketErrorCode.UNSUPPORTED_TYPE,
    ])

    return userActionRequired.has(code)
  }

  /**
   * 获取建议的操作
   *
   * @param code 错误码
   * @returns 建议的操作文本
   */
  private getSuggestedAction(code: WebSocketErrorCode): string | undefined {
    const actions: Partial<Record<WebSocketErrorCode, string>> = {
      [WebSocketErrorCode.AUTH_FAILED]: '请点击重新登录',
      [WebSocketErrorCode.CONNECTION_LIMIT]: '请刷新页面或稍后重试',
      [WebSocketErrorCode.MESSAGE_TOO_LARGE]: '请减少消息内容长度',
      [WebSocketErrorCode.INVALID_FORMAT]: '请检查输入格式',
      [WebSocketErrorCode.UNREACHABLE]: '请检查网络连接',
    }

    return actions[code]
  }

  /**
   * 更新重试策略
   *
   * @param policy 部分重试策略配置
   */
  updateRetryPolicy(policy: Partial<RetryPolicy>): void {
    this.retryPolicy = {
      ...this.retryPolicy,
      ...policy,
    }
  }

  /**
   * 设置错误上报器
   *
   * @param reporter 错误上报器实例
   */
  setErrorReporter(reporter: ErrorReporter): void {
    this.errorReporter = reporter
  }

  /**
   * 上报指标数据
   *
   * @param name 指标名称
   * @param value 指标值
   * @param tags 标签
   */
  async reportMetric(name: string, value: number, tags?: Record<string, string>): Promise<void> {
    await this.errorReporter.reportMetric(name, value, tags)
  }
}

/**
 * 创建WebSocket错误处理器
 *
 * @param retryPolicy 部分重试策略配置
 * @param _enableSentry 是否启用Sentry上报（保留参数，暂未使用）
 * @returns WebSocket错误处理器实例
 */
export function createWebSocketErrorHandler(
  retryPolicy?: Partial<RetryPolicy>,
  _enableSentry?: boolean,
): WebSocketErrorHandler {
  const policy = retryPolicy ? { ...DEFAULT_RETRY_POLICY, ...retryPolicy } : DEFAULT_RETRY_POLICY
  const reporter = new ConsoleErrorReporter()

  return new WebSocketErrorHandler(policy, reporter)
}

/** 默认错误处理器实例 */
export const defaultErrorHandler = createWebSocketErrorHandler()
