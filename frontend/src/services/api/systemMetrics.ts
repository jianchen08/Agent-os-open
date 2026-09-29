/**
 * 系统级内存指标 API 服务
 *
 * GET /api/v1/system/memstats（内核自有只读端点，admin 鉴权与 plugins/hosts
 * 同法）：监控页全口径内存的内核段数据源——快照里的 OS 口径内核进程常驻
 * 内存（process.rss_current），与插件宿主进程 RSS 同口径可加和。
 */

import { API_ENDPOINTS } from '@/constants/api'
import apiClient from '@/services/api/client'
import { requestWithRetry } from '@/utils/retry'
import type { RetryOptions } from '@/utils/retry'

/** 内核 memstats 快照（只声明前端消费的 OS 口径进程段；字段缺失兜底 null） */
export interface KernelMemStats {
  /** 内核进程常驻内存（字节）；采集失败为 null */
  process_rss_bytes: number | null
}

export async function getKernelMemStats(options: RetryOptions = {}): Promise<KernelMemStats> {
  return requestWithRetry(async () => {
    const response = await apiClient.get<KernelMemStats>(API_ENDPOINTS.SYSTEM.MEMSTATS)
    return { process_rss_bytes: response.data.process_rss_bytes ?? null }
  }, options)
}
