/**
 * WebSocket 心跳管理器
 *
 * 负责管理WebSocket连接的心跳机制，监控网络质量
 */

/**
 * 心跳回调接口
 */
export interface HeartbeatCallbacks {
  /** 发送心跳回调 */
  onSendHeartbeat: () => Promise<void>
  /** 心跳响应回调 */
  onHeartbeatResponse: (rtt: number) => void
  /** 心跳超时回调 */
  onHeartbeatTimeout: () => void
  /** 网络质量变化回调 */
  onNetworkQualityChange: (quality: string) => void
}

/**
 * 网络统计信息
 */
export interface NetworkStats {
  /** 平均往返时间（毫秒） */
  averageRtt: number
  /** 最小往返时间（毫秒） */
  minRtt: number
  /** 最大往返时间（毫秒） */
  maxRtt: number
  /** 心跳成功次数 */
  successCount: number
  /** 心跳失败次数 */
  failureCount: number
  /** 网络质量评级 */
  quality: string
}

/**
 * 心跳管理器（简化版本）
 */
export class HeartbeatManager {
  /** 回调函数集合（保留用于未来扩展） */
  private _callbacks: HeartbeatCallbacks | null = null

  /** 网络统计信息 */
  private stats: NetworkStats = {
    averageRtt: 0,
    minRtt: 0,
    maxRtt: 0,
    successCount: 0,
    failureCount: 0,
    quality: 'unknown',
  }

  /**
   * 设置回调函数
   *
   * @param callbacks 心跳回调函数集合
   */
  setCallbacks(callbacks: HeartbeatCallbacks): void {
    this._callbacks = callbacks
  }

  /**
   * 启动心跳管理器
   */
  start(): void {
    console.log('[HeartbeatManager] 心跳管理器已启动（简化版本）')
  }

  /**
   * 停止心跳管理器
   */
  stop(): void {
    console.log('[HeartbeatManager] 心跳管理器已停止')
  }

  /**
   * 获取网络统计信息
   *
   * @returns 网络统计信息的副本
   */
  getNetworkStats(): NetworkStats {
    return { ...this.stats }
  }

  /**
   * 获取网络质量
   *
   * @returns 网络质量评级字符串
   */
  getNetworkQuality(): string {
    return this.stats.quality
  }

  /**
   * 获取当前心跳间隔
   *
   * @returns 心跳间隔（毫秒）
   */
  getCurrentInterval(): number {
    return 30000 // 30秒默认间隔
  }

  /**
   * 重置统计信息
   */
  reset(): void {
    this.stats = {
      averageRtt: 0,
      minRtt: 0,
      maxRtt: 0,
      successCount: 0,
      failureCount: 0,
      quality: 'unknown',
    }
    console.log('[HeartbeatManager] 统计信息已重置')
  }
}
