/**
 * 统一日志服务
 *
 * 提供分级日志功能，支持生产环境控制
 *
 * 日志级别：
 * - ERROR: 错误需要关注（生产环境必须记录）
 * - WARN: 警告需关注（生产环境记录）
 * - INFO: 关键流程节点（生产环境记录）
 * - DEBUG: 调试信息（生产环境可关闭）
 * - VERBOSE: 详细追踪（生产环境可关闭）
 *
 * 使用方式：
 * ```typescript
 * import { logger } from '@/utils/logger'
 *
 * // 创建模块日志器
 * const log = logger.module('WebSocket')
 *
 * log.error('连接失败', error)
 * log.warn('重试连接', { attempt: 3 })
 * log.info('连接已建立', { threadId: 'xxx' })
 * log.debug('发送消息', { type: 'user_input' })
 * log.verbose('消息详情', messageData)
 * ```
 *
 * @module utils/logger
 */

/**
 * 日志级别枚举
 */
export enum LogLevel {
  /** 错误需要关注 */
  ERROR = 0,
  /** 警告需关注 */
  WARN = 1,
  /** 关键流程节点 */
  INFO = 2,
  /** 调试信息 */
  DEBUG = 3,
  /** 详细追踪 */
  VERBOSE = 4,
}

/**
 * 日志级别配置
 */
interface LogConfig {
  /** 当前日志级别 */
  level: LogLevel
  /** 是否启用日志 */
  enabled: boolean
  /** 是否显示时间戳 */
  showTimestamp: boolean
  /** 是否显示模块名 */
  showModule: boolean
  /** 是否在生产环境显示 DEBUG 和 VERBOSE */
  debugInProduction: boolean
}

/**
 * 默认配置
 */
const DEFAULT_CONFIG: LogConfig = {
  level: LogLevel.INFO,
  enabled: true,
  showTimestamp: true,
  showModule: true,
  debugInProduction: false,
}

/**
 * 日志管理器
 */
class LogManager {
  private config: LogConfig
  private moduleCache: Map<string, ModuleLogger> = new Map()

  constructor() {
    // 根据环境变量设置日志级别
    const isProduction = import.meta.env.PROD
    this.config = {
      ...DEFAULT_CONFIG,
      level: isProduction ? LogLevel.INFO : LogLevel.DEBUG,
      debugInProduction: false,
    }
  }

  /**
   * 设置日志级别
   */
  setLevel(level: LogLevel): void {
    this.config.level = level
  }

  /**
   * 获取当前日志级别
   */
  getLevel(): LogLevel {
    return this.config.level
  }

  /**
   * 启用/禁用日志
   */
  setEnabled(enabled: boolean): void {
    this.config.enabled = enabled
  }

  /**
   * 设置是否在生产环境显示调试日志
   */
  setDebugInProduction(enabled: boolean): void {
    this.config.debugInProduction = enabled
  }

  /**
   * 检查日志级别是否应该输出
   */
  shouldLog(level: LogLevel): boolean {
    if (!this.config.enabled) {
      return false
    }

    // 生产环境：DEBUG 和 VERBOSE 默认不输出
    if (import.meta.env.PROD && !this.config.debugInProduction) {
      if (level >= LogLevel.DEBUG) {
        return false
      }
    }

    return level <= this.config.level
  }

  /**
   * 格式化日志前缀
   */
  formatPrefix(moduleName: string, level: LogLevel): string {
    const parts: string[] = []

    // 添加时间戳
    if (this.config.showTimestamp) {
      parts.push(`[${new Date().toISOString().split('T')[1].split('.')[0]}]`)
    }

    // 添加模块名
    if (this.config.showModule && moduleName) {
      parts.push(`[${moduleName}]`)
    }

    // 添加级别标识
    const levelMap: Record<LogLevel, string> = {
      [LogLevel.ERROR]: '❌ ERROR',
      [LogLevel.WARN]: '⚠️ WARN',
      [LogLevel.INFO]: 'ℹ️ INFO',
      [LogLevel.DEBUG]: '🔍 DEBUG',
      [LogLevel.VERBOSE]: '📝 VERBOSE',
    }
    parts.push(levelMap[level])

    return parts.join(' ')
  }

  /**
   * 创建模块日志器
   */
  module(moduleName: string): ModuleLogger {
    // 使用缓存避免重复创建
    if (this.moduleCache.has(moduleName)) {
      return this.moduleCache.get(moduleName)!
    }

    const moduleLogger = new ModuleLogger(this, moduleName)
    this.moduleCache.set(moduleName, moduleLogger)
    return moduleLogger
  }

  /**
   * 清除模块缓存
   */
  clearCache(): void {
    this.moduleCache.clear()
  }
}

/**
 * 模块日志器
 */
class ModuleLogger {
  private manager: LogManager
  private moduleName: string

  constructor(manager: LogManager, moduleName: string) {
    this.manager = manager
    this.moduleName = moduleName
  }

  /**
   * 记录错误日志
   *
   * @param message 日志消息
   * @param data 附加数据（可选）
   */
  error(message: string, ...args: unknown[]): void {
    if (!this.manager.shouldLog(LogLevel.ERROR)) return
    const prefix = this.manager.formatPrefix(this.moduleName, LogLevel.ERROR)
    console.error(prefix, this._format(message, args))
  }

  /**
   * 记录警告日志
   *
   * @param message 日志消息，支持 printf 格式化（%s/%d/%j）
   * @param args 格式化参数
   */
  warn(message: string, ...args: unknown[]): void {
    if (!this.manager.shouldLog(LogLevel.WARN)) return
    const prefix = this.manager.formatPrefix(this.moduleName, LogLevel.WARN)
    console.warn(prefix, this._format(message, args))
  }

  /**
   * 记录信息日志
   *
   * @param message 日志消息，支持 printf 格式化（%s/%d/%j）
   * @param args 格式化参数
   */
  info(message: string, ...args: unknown[]): void {
    if (!this.manager.shouldLog(LogLevel.INFO)) return
    const prefix = this.manager.formatPrefix(this.moduleName, LogLevel.INFO)
    console.log(prefix, this._format(message, args))
  }

  /**
   * 记录调试日志
   *
   * @param message 日志消息，支持 printf 格式化（%s/%d/%j）
   * @param args 格式化参数
   */
  debug(message: string, ...args: unknown[]): void {
    if (!this.manager.shouldLog(LogLevel.DEBUG)) return
    const prefix = this.manager.formatPrefix(this.moduleName, LogLevel.DEBUG)
    console.log(prefix, this._format(message, args))
  }

  /**
   * 记录详细日志
   *
   * @param message 日志消息，支持 printf 格式化（%s/%d/%j）
   * @param args 格式化参数
   */
  verbose(message: string, ...args: unknown[]): void {
    if (!this.manager.shouldLog(LogLevel.VERBOSE)) return
    const prefix = this.manager.formatPrefix(this.moduleName, LogLevel.VERBOSE)
    console.log(prefix, this._format(message, args))
  }

  /**
   * 简易 printf 格式化：将 %s/%d/%j 替换为对应参数值
   *
   * 无占位符时直接返回原始 message + 追加参数。
   *
   * @param template 模板字符串
   * @param args 格式化参数列表
   * @returns 格式化后的字符串
   */
  private _format(template: string, args: unknown[]): string {
    if (args.length === 0) return template
    let idx = 0
    const result = template.replace(/%[sdj]/g, (match) => {
      if (idx >= args.length) return match
      const val = args[idx++]
      if (match === '%d') return String(typeof val === 'number' ? val : Number(val) || 0)
      if (match === '%j') {
        try { return JSON.stringify(val) } catch { return '[Circular]' }
      }
      return String(val ?? '')
    })
    if (idx < args.length) {
      return result + ' ' + args.slice(idx).map((a) => String(a ?? '')).join(' ')
    }
    return result
  }

  /**
   * 创建子模块日志器
   *
   * @param subModuleName 子模块名
   */
  subModule(subModuleName: string): ModuleLogger {
    return this.manager.module(`${this.moduleName}:${subModuleName}`)
  }
}

// 创建全局日志管理器实例
const logManager = new LogManager()

/**
 * 全局日志器
 */
export const logger = {
  /**
   * 创建模块日志器
   */
  module: (moduleName: string): ModuleLogger => logManager.module(moduleName),

  /**
   * 设置日志级别
   */
  setLevel: (level: LogLevel): void => logManager.setLevel(level),

  /**
   * 获取当前日志级别
   */
  getLevel: (): LogLevel => logManager.getLevel(),

  /**
   * 启用/禁用日志
   */
  setEnabled: (enabled: boolean): void => logManager.setEnabled(enabled),

  /**
   * 设置是否在生产环境显示调试日志
   */
  setDebugInProduction: (enabled: boolean): void => logManager.setDebugInProduction(enabled),

  /**
   * LogLevel 枚举
   */
  LogLevel,
}

/**
 * 预定义的模块日志器
 */
export const loggers = {
  /** WebSocket 服务日志器 */
  websocket: logManager.module('WebSocket'),
  /** 消息合并日志器 */
  messageMerger: logManager.module('MessageMerger'),
  /** 思考模式日志器 */
  thinkingMode: logManager.module('ThinkingMode'),
  /** 消息操作日志器 */
  messageActions: logManager.module('MessageActions'),
  /** 会话存储日志器 */
  sessionStore: logManager.module('SessionStore'),
  /** 执行卡片日志器 */
  executionCard: logManager.module('ExecutionCard'),
  /** 工具调用日志器 */
  toolCall: logManager.module('ToolCall'),
  /** 连接管理日志器 */
  connectionManager: logManager.module('ConnectionManager'),
  /** 性能监控日志器 */
  performance: logManager.module('Performance'),
  /** 通知服务日志器 */
  notification: logManager.module('Notification'),
  /** API 客户端日志器 */
  apiClient: logManager.module('APIClient'),
  /** 主题服务日志器 */
  themeService: logManager.module('ThemeService'),
  /** 存储服务日志器 */
  storage: logManager.module('Storage'),
  /** 错误处理日志器 */
  errorHandler: logManager.module('ErrorHandler'),
  /** SSE 服务日志器 */
  sseService: logManager.module('SSEService'),
  /** 心跳管理日志器 */
  heartbeat: logManager.module('Heartbeat'),
  /** 消息队列日志器 */
  messageQueue: logManager.module('MessageQueue'),
  /** 重连管理日志器 */
  reconnect: logManager.module('Reconnect'),
  /** 事件处理日志器 */
  eventHandler: logManager.module('EventHandler'),
}

/**
 * 导出类型
 */
export type { ModuleLogger }
