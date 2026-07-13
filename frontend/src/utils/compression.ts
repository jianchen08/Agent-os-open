/**
 * WebSocket 消息压缩工具
 *
 * 简化版本，暂时禁用压缩功能，避免pako依赖问题
 */

/**
 * 压缩配置
 */
export interface CompressionConfig {
  /** 压缩阈值（字节），超过此大小才压缩 */
  threshold: number
  /** 压缩级别 (1-9)，9为最高压缩率 */
  level: number
  /** 是否启用压缩 */
  enabled: boolean
}

/**
 * 压缩结果
 */
export interface CompressionResult {
  /** 是否已压缩 */
  compressed: boolean
  /** 压缩后的数据 */
  data: Uint8Array
  /** 原始大小 */
  originalSize: number
  /** 压缩后大小 */
  compressedSize: number
  /** 压缩率（0-1） */
  compressionRatio: number
  /** 压缩耗时（毫秒） */
  compressionTime: number
}

/**
 * 解压结果
 */
export interface DecompressionResult {
  /** 解压后的数据 */
  data: string
  /** 压缩大小 */
  compressedSize: number
  /** 解压后大小 */
  decompressedSize: number
  /** 解压耗时（毫秒） */
  decompressionTime: number
}

/**
 * 默认压缩配置（暂时禁用压缩）
 */
const DEFAULT_CONFIG: CompressionConfig = {
  threshold: 1024, // 1KB
  level: 6, // 平衡压缩率和速度
  enabled: false, // 暂时禁用压缩
}

/**
 * 压缩标记字节
 * 0x01 表示数据已压缩
 * 0x00 表示数据未压缩
 */
const COMPRESSION_MARKER = {
  COMPRESSED: 0x01,
  UNCOMPRESSED: 0x00,
} as const

/**
 * WebSocket 消息压缩器（简化版本）
 */
export class MessageCompressor {
  private config: CompressionConfig

  constructor(config?: Partial<CompressionConfig>) {
    this.config = {
      ...DEFAULT_CONFIG,
      ...config,
      enabled: false, // 强制禁用压缩
    }
  }

  /**
   * 压缩消息（简化版本，不进行实际压缩）
   *
   * @param message 要压缩的消息对象
   * @returns 压缩结果
   */
  compress(message: unknown): CompressionResult {
    const startTime = performance.now()

    // 序列化消息
    const jsonString = JSON.stringify(message)
    const originalData = new TextEncoder().encode(jsonString)
    const originalSize = originalData.length

    // 不压缩，添加未压缩标记
    const result = new Uint8Array(originalSize + 1)
    result[0] = COMPRESSION_MARKER.UNCOMPRESSED
    result.set(originalData, 1)

    const compressionTime = performance.now() - startTime

    return {
      compressed: false,
      data: result,
      originalSize,
      compressedSize: result.length,
      compressionRatio: 0,
      compressionTime,
    }
  }

  /**
   * 解压消息（简化版本）
   *
   * @param data 压缩的数据
   * @returns 解压结果
   */
  decompress(data: Uint8Array): DecompressionResult {
    const startTime = performance.now()
    const compressedSize = data.length

    if (data.length === 0) {
      throw new Error('数据为空')
    }

    // 检查压缩标记
    const marker = data[0]
    const payload = data.slice(1)

    if (marker === COMPRESSION_MARKER.UNCOMPRESSED) {
      // 未压缩数据，直接解码
      const decompressed = new TextDecoder().decode(payload)
      const decompressionTime = performance.now() - startTime

      return {
        data: decompressed,
        compressedSize,
        decompressedSize: payload.length,
        decompressionTime,
      }
    } else if (marker === COMPRESSION_MARKER.COMPRESSED) {
      // 压缩数据，但由于禁用了压缩，这种情况不应该出现
      throw new Error('检测到压缩数据，但压缩功能已禁用')
    } else {
      throw new Error(`未知的压缩标记: ${marker}`)
    }
  }

  /**
   * 检查数据是否已压缩
   *
   * @param data 数据
   * @returns 是否已压缩
   */
  isCompressed(data: Uint8Array): boolean {
    return data.length > 0 && data[0] === COMPRESSION_MARKER.COMPRESSED
  }

  /**
   * 更新配置
   *
   * @param config 新配置
   */
  updateConfig(config: Partial<CompressionConfig>): void {
    this.config = {
      ...this.config,
      ...config,
      enabled: false, // 强制禁用压缩
    }

  }

  /**
   * 获取当前配置
   */
  getConfig(): CompressionConfig {
    return { ...this.config }
  }

  /**
   * 估算压缩后大小（简化版本）
   *
   * @param message 消息对象
   * @returns 估算的压缩后大小
   */
  estimateCompressedSize(message: unknown): number {
    const jsonString = JSON.stringify(message)
    const originalSize = new TextEncoder().encode(jsonString).length
    return originalSize + 1 // 未压缩标记
  }
}

/**
 * 全局压缩器实例
 */
let globalCompressor: MessageCompressor | null = null

/**
 * 获取全局压缩器实例
 */
export function getMessageCompressor(): MessageCompressor {
  if (!globalCompressor) {
    globalCompressor = new MessageCompressor()
  }
  return globalCompressor
}

/**
 * 初始化全局压缩器
 *
 * @param config 压缩配置
 */
export function initMessageCompressor(config?: Partial<CompressionConfig>): MessageCompressor {
  globalCompressor = new MessageCompressor(config)
  return globalCompressor
}

/**
 * 压缩消息（便捷函数）
 *
 * @param message 消息对象
 * @returns 压缩结果
 */
export function compressMessage(message: unknown): CompressionResult {
  return getMessageCompressor().compress(message)
}

/**
 * 解压消息（便捷函数）
 *
 * @param data 压缩数据
 * @returns 解压后的消息对象
 */
export function decompressMessage(data: Uint8Array): unknown {
  const result = getMessageCompressor().decompress(data)
  return JSON.parse(result.data)
}

/**
 * 检查是否需要压缩
 *
 * @param message 消息对象
 * @param threshold 压缩阈值（可选）
 * @returns 是否需要压缩
 */
export function shouldCompress(message: unknown, threshold?: number): boolean {
  const compressor = getMessageCompressor()
  const config = compressor.getConfig()
  const actualThreshold = threshold ?? config.threshold

  if (!config.enabled) {
    return false
  }

  const jsonString = JSON.stringify(message)
  const size = new TextEncoder().encode(jsonString).length

  return size >= actualThreshold
}

/**
 * 获取消息大小（字节）
 *
 * @param message 消息对象
 * @returns 消息大小
 */
export function getMessageSize(message: unknown): number {
  const jsonString = JSON.stringify(message)
  return new TextEncoder().encode(jsonString).length
}
