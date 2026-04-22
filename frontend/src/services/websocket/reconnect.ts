/**
 * WebSocket 重连机制模块
 *
 * 提供指数退避重连策略和重连状态管理
 */

import { WS_RECONNECT_CONFIG, WebSocketErrorCode } from '@/constants/websocket'

/**
 * 重连配置接口
 */
export interface ReconnectConfig {
  /** 最大重连次数 */
  maxRetries: number
  /** 初始重连延迟（毫秒） */
  initialDelay: number
  /** 最大重连延迟（毫秒） */
  maxDelay: number
  /** 延迟增长因子 */
  backoffFactor: number
}

/**
 * 重连状态
 */
export type ReconnectState = 'idle' | 'waiting' | 'connecting' | 'failed'

/**
 * 重连回调接口
 */
export interface ReconnectCallbacks {
  /** 开始重连时调用 */
  onReconnectStart?: (attempt: number, delay: number) => void
  /** 重连成功时调用 */
  onReconnectSuccess?: () => void
  /** 重连失败时调用 */
  onReconnectFailed?: (attempt: number, error?: Error) => void
  /** 达到最大重连次数时调用 */
  onMaxRetriesReached?: () => void
}

/**
 * 不应触发自动重连的关闭码集合
 *
 * 这些关闭码表示连接被有意关闭或需要用户手动干预
 */
const NO_RECONNECT_CLOSE_CODES = new Set([
  4001, // 认证失败
  4002, // 令牌过期
  4003, // 连接数超限
  4004, // 连接被新连接替换
  1000, // 正常关闭
])

/**
 * 重连管理器
 *
 * 管理 WebSocket 重连逻辑，支持指数退避策略
 */
export class ReconnectManager {
  /** 重连配置 */
  private config: ReconnectConfig

  /** 当前重连尝试次数 */
  private attempts: number = 0

  /** 重连定时器 */
  private timer: ReturnType<typeof setTimeout> | null = null

  /** 当前状态 */
  private state: ReconnectState = 'idle'

  /** 回调函数 */
  private callbacks: ReconnectCallbacks = {}

  /** 是否已取消 */
  private cancelled: boolean = false

  /**
   * 创建重连管理器
   *
   * @param config 重连配置（可选，使用默认配置）
   */
  constructor(config?: Partial<ReconnectConfig>) {
    this.config = {
      maxRetries: config?.maxRetries ?? WS_RECONNECT_CONFIG.MAX_RETRIES,
      initialDelay: config?.initialDelay ?? WS_RECONNECT_CONFIG.INITIAL_DELAY,
      maxDelay: config?.maxDelay ?? WS_RECONNECT_CONFIG.MAX_DELAY,
      backoffFactor:
        config?.backoffFactor ?? WS_RECONNECT_CONFIG.BACKOFF_FACTOR,
    }
  }

  /**
   * 设置回调函数
   *
   * @param callbacks 回调函数对象
   */
  setCallbacks(callbacks: ReconnectCallbacks): void {
    this.callbacks = callbacks
  }

  /**
   * 计算下一次重连延迟
   *
   * 使用指数退避算法：delay = min(initialDelay * backoffFactor^attempt, maxDelay)
   *
   * @param attempt 当前尝试次数（从 0 开始）
   * @returns 延迟时间（毫秒）
   */
  calculateDelay(attempt: number): number {
    const delay =
      this.config.initialDelay * Math.pow(this.config.backoffFactor, attempt)
    return Math.min(delay, this.config.maxDelay)
  }

  /**
   * 检查是否应该重连
   *
   * 根据WebSocket关闭码判断是否需要自动重连
   *
   * @param closeCode WebSocket 关闭码
   * @returns 是否应该重连
   */
  shouldReconnect(closeCode: number): boolean {
    // 检查是否在不应该重连的关闭码集合中
    if (NO_RECONNECT_CLOSE_CODES.has(closeCode)) {
      return false
    }

    // 检查是否超过最大重连次数
    if (this.attempts >= this.config.maxRetries) {
      return false
    }

    return true
  }

  /**
   * 检查是否可以继续重连
   *
   * @returns 是否可以继续重连
   */
  canRetry(): boolean {
    return this.attempts < this.config.maxRetries && !this.cancelled
  }

  /**
   * 调度重连
   *
   * @param connectFn 连接函数
   * @returns Promise，在重连完成或失败时 resolve
   */
  async scheduleReconnect(connectFn: () => Promise<boolean>): Promise<boolean> {
    if (this.cancelled) {
      return false
    }

    if (!this.canRetry()) {
      this.state = 'failed'
      this.callbacks.onMaxRetriesReached?.()
      return false
    }

    const delay = this.calculateDelay(this.attempts)
    this.state = 'waiting'

    console.log(
      `[Reconnect] 将在 ${delay}ms 后进行第 ${this.attempts + 1} 次重连`
    )

    this.callbacks.onReconnectStart?.(this.attempts + 1, delay)

    return new Promise(resolve => {
      this.timer = setTimeout(async () => {
        if (this.cancelled) {
          resolve(false)
          return
        }

        this.state = 'connecting'
        this.attempts++

        try {
          const success = await connectFn()

          if (success) {
            this.reset()
            this.callbacks.onReconnectSuccess?.()
            resolve(true)
          } else {
            this.callbacks.onReconnectFailed?.(this.attempts)
            // 继续尝试重连
            const result = await this.scheduleReconnect(connectFn)
            resolve(result)
          }
        } catch (error) {
          this.callbacks.onReconnectFailed?.(
            this.attempts,
            error instanceof Error ? error : new Error(String(error))
          )
          // 继续尝试重连
          const result = await this.scheduleReconnect(connectFn)
          resolve(result)
        }
      }, delay)
    })
  }

  /**
   * 立即尝试重连（不等待延迟）
   *
   * @param connectFn 连接函数
   * @returns Promise，在重连完成或失败时 resolve
   */
  async reconnectNow(connectFn: () => Promise<boolean>): Promise<boolean> {
    if (this.cancelled) {
      return false
    }

    if (!this.canRetry()) {
      this.state = 'failed'
      this.callbacks.onMaxRetriesReached?.()
      return false
    }

    this.state = 'connecting'
    this.attempts++

    this.callbacks.onReconnectStart?.(this.attempts, 0)

    try {
      const success = await connectFn()

      if (success) {
        this.reset()
        this.callbacks.onReconnectSuccess?.()
        return true
      } else {
        this.callbacks.onReconnectFailed?.(this.attempts)
        return false
      }
    } catch (error) {
      this.callbacks.onReconnectFailed?.(
        this.attempts,
        error instanceof Error ? error : new Error(String(error))
      )
      return false
    }
  }

  /**
   * 取消重连
   */
  cancel(): void {
    this.cancelled = true
    this.clearTimer()
    this.state = 'idle'
  }

  /**
   * 重置重连状态
   */
  reset(): void {
    this.attempts = 0
    this.cancelled = false
    this.clearTimer()
    this.state = 'idle'
  }

  /**
   * 清除定时器
   */
  private clearTimer(): void {
    if (this.timer) {
      clearTimeout(this.timer)
      this.timer = null
    }
  }

  /**
   * 获取当前状态
   *
   * @returns 当前重连状态
   */
  getState(): ReconnectState {
    return this.state
  }

  /**
   * 获取当前尝试次数
   *
   * @returns 当前重连尝试次数
   */
  getAttempts(): number {
    return this.attempts
  }

  /**
   * 获取最大重连次数
   *
   * @returns 最大重连次数
   */
  getMaxRetries(): number {
    return this.config.maxRetries
  }

  /**
   * 获取配置
   *
   * @returns 重连配置的只读副本
   */
  getConfig(): Readonly<ReconnectConfig> {
    return { ...this.config }
  }

  /**
   * 更新配置
   *
   * @param config 新配置（部分）
   */
  updateConfig(config: Partial<ReconnectConfig>): void {
    this.config = {
      ...this.config,
      ...config,
    }
  }
}

/**
 * 创建重连管理器实例
 *
 * @param config 重连配置（可选）
 * @returns 重连管理器实例
 */
export function createReconnectManager(
  config?: Partial<ReconnectConfig>
): ReconnectManager {
  return new ReconnectManager(config)
}

/**
 * 计算指数退避延迟（静态工具函数）
 *
 * @param attempt 当前尝试次数（从 0 开始）
 * @param config 配置（可选）
 * @returns 延迟时间（毫秒）
 */
export function calculateBackoffDelay(
  attempt: number,
  config?: Partial<ReconnectConfig>
): number {
  const initialDelay = config?.initialDelay ?? WS_RECONNECT_CONFIG.INITIAL_DELAY
  const maxDelay = config?.maxDelay ?? WS_RECONNECT_CONFIG.MAX_DELAY
  const backoffFactor =
    config?.backoffFactor ?? WS_RECONNECT_CONFIG.BACKOFF_FACTOR

  const delay = initialDelay * Math.pow(backoffFactor, attempt)
  return Math.min(delay, maxDelay)
}
