/**
 * 统一数据层 - 数据源解析器
 *
 * 解析 module://collection 格式的数据引用，自动构建查询
 * 支持 fetch + 缓存 + polling
 */

import apiClient from '@/services/api/client'
import { parseDataSourceRef, resolveDataSource } from '@/services/schema/parser'

/** 缓存条目 */
interface CacheEntry<T> {
  data: T
  timestamp: number
  ttl: number
}

class DataSourceResolver {
  private cache: Map<string, CacheEntry<unknown>> = new Map()
  private pollingTimers: Map<string, ReturnType<typeof setInterval>> = new Map()
  private defaultTTL = 30000

  /**
   * 获取数据
   *
   * @param ref - 数据源引用字符串，格式 module://collection
   * @param options - 可选配置：ttl 缓存时间、forceRefresh 强制刷新
   * @returns 解析后的数据
   */
  async fetch<T>(ref: string, options?: { ttl?: number; forceRefresh?: boolean }): Promise<T> {
    const parsed = parseDataSourceRef(ref)
    const resolved = resolveDataSource(parsed)
    const cacheKey = this.getCacheKey(ref)

    if (!options?.forceRefresh) {
      const cached = this.getFromCache<T>(cacheKey)
      if (cached !== undefined) return cached
    }

    const response = await apiClient.get(resolved.endpoint, { params: resolved.params })
    const data = response.data as T

    this.cache.set(cacheKey, {
      data,
      timestamp: Date.now(),
      ttl: options?.ttl ?? this.defaultTTL,
    })

    return data
  }

  /**
   * 启动轮询
   *
   * @param ref - 数据源引用字符串
   * @param callback - 数据回调函数
   * @param interval - 轮询间隔（毫秒），默认 5000
   */
  startPolling<T>(ref: string, callback: (data: T) => void, interval = 5000): void {
    this.stopPolling(ref)

    const timer = setInterval(async () => {
      try {
        const data = await this.fetch<T>(ref, { forceRefresh: true })
        callback(data)
      } catch {
        /* 轮询错误静默处理 */
      }
    }, interval)

    this.pollingTimers.set(ref, timer)
  }

  /**
   * 停止轮询
   *
   * @param ref - 数据源引用字符串
   */
  stopPolling(ref: string): void {
    const timer = this.pollingTimers.get(ref)
    if (timer) {
      clearInterval(timer)
      this.pollingTimers.delete(ref)
    }
  }

  /**
   * 清空缓存
   */
  clearCache(): void {
    this.cache.clear()
  }

  /**
   * 停止所有轮询
   */
  stopAll(): void {
    this.pollingTimers.forEach((timer) => clearInterval(timer))
    this.pollingTimers.clear()
  }

  /**
   * 生成缓存键
   */
  private getCacheKey(ref: string): string {
    return ref
  }

  /**
   * 从缓存获取数据
   */
  private getFromCache<T>(key: string): T | undefined {
    const entry = this.cache.get(key)
    if (!entry) return undefined
    if (Date.now() - entry.timestamp > entry.ttl) {
      this.cache.delete(key)
      return undefined
    }
    return entry.data as T
  }
}

/** 数据源解析器单例 */
export const dataSourceResolver = new DataSourceResolver()
