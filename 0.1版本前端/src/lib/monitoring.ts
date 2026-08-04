/**
 * WebSocket 监控模块
 *
 * 提供 WebSocket 连接状态监控和指标采集功能。
 */

interface WebSocketMonitorMetrics {
  /** 连接数 */
  connectionCount: number
  /** 消息发送计数 */
  messagesSent: number
  /** 消息接收计数 */
  messagesReceived: number
  /** 消息发送失败计数 */
  messagesFailed: number
  /** 平均延迟（ms） */
  averageLatency: number
  /** 心跳平均延迟（ms） */
  heartbeatLatency: number
  /** 连接开始时间戳 */
  connectionStartTime: number | null
  /** 重连次数 */
  reconnectCount: number
  /** 错误计数 */
  errorCount: number
  /** 最后活跃时间 */
  lastActiveTime: number | null
}

class WebSocketMonitor {
  private metrics: WebSocketMonitorMetrics = {
    connectionCount: 0,
    messagesSent: 0,
    messagesReceived: 0,
    messagesFailed: 0,
    averageLatency: 0,
    heartbeatLatency: 0,
    connectionStartTime: null,
    reconnectCount: 0,
    errorCount: 0,
    lastActiveTime: null,
  }

  /** 累计延迟（用于计算平均值） */
  private totalLatency = 0
  private latencyCount = 0

  /** 累计心跳延迟 */
  private totalHeartbeatLatency = 0
  private heartbeatLatencyCount = 0

  /** 记录连接开始 */
  recordConnectionStart(): void {
    this.metrics.connectionCount++
    this.metrics.connectionStartTime = Date.now()
    this.metrics.lastActiveTime = Date.now()
  }

  /** 记录连接结束 */
  recordConnectionEnd(failed = false): void {
    this.metrics.connectionCount = Math.max(0, this.metrics.connectionCount - 1)
    this.metrics.connectionStartTime = null
    if (failed) {
      this.metrics.errorCount++
    }
  }

  /** 记录消息发送 */
  recordMessageSent(type: string, size: number): void {
    this.metrics.messagesSent++
    this.metrics.lastActiveTime = Date.now()
  }

  /** 记录消息发送失败 */
  recordMessageFailed(): void {
    this.metrics.messagesFailed++
    this.metrics.lastActiveTime = Date.now()
  }

  /** 记录消息接收 */
  recordMessageReceived(type: string, size: number): void {
    this.metrics.messagesReceived++
    this.metrics.lastActiveTime = Date.now()
  }

  /** 记录心跳延迟 */
  recordHeartbeatLatency(latency: number): void {
    this.heartbeatLatencyCount++
    this.totalHeartbeatLatency += latency
    this.metrics.heartbeatLatency =
      this.totalHeartbeatLatency / this.heartbeatLatencyCount
  }

  /** 记录重连 */
  recordReconnect(): void {
    this.metrics.reconnectCount++
  }

  /** 记录错误 */
  recordError(category: string, message: string): void {
    this.metrics.errorCount++
    this.metrics.lastActiveTime = Date.now()
  }

  /** 获取当前监控指标 */
  getMetrics(): WebSocketMonitorMetrics {
    return { ...this.metrics }
  }

  /** 重置监控指标 */
  reset(): void {
    this.metrics = {
      connectionCount: 0,
      messagesSent: 0,
      messagesReceived: 0,
      messagesFailed: 0,
      averageLatency: 0,
      heartbeatLatency: 0,
      connectionStartTime: null,
      reconnectCount: 0,
      errorCount: 0,
      lastActiveTime: null,
    }
    this.totalLatency = 0
    this.latencyCount = 0
    this.totalHeartbeatLatency = 0
    this.heartbeatLatencyCount = 0
  }

  /** 导出监控数据 */
  exportData(): Record<string, unknown> {
    return {
      metrics: this.getMetrics(),
      exportedAt: new Date().toISOString(),
    }
  }
}

/** 全局单例 */
let monitorInstance: WebSocketMonitor | null = null

/**
 * 获取 WebSocket 监控实例
 * 使用懒加载单例模式
 */
export function getWebSocketMonitor(): WebSocketMonitor {
  if (!monitorInstance) {
    monitorInstance = new WebSocketMonitor()
  }
  return monitorInstance
}
