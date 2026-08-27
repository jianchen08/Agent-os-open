/**
 * LLM Payload 诊断 API 服务
 *
 * 数据源：monitoring 插件 payload-diag（快照由 llm adapter 在
 * 每次 LLM 请求前落盘，含真实发送的 model + messages 请求体）。
 * 开关：环境变量 AGENTOS_PAYLOAD_DIAG=1（默认关闭）。
 */

import apiClient from '@/services/api/client'
import { MONITORING_ENDPOINTS } from './endpoints.generated'

/** payload 快照元数据（从文件名解析，无需读文件） */
export interface PayloadDiagItem {
  /** 文件名（读取内容的凭证） */
  name: string
  /** 毫秒时间戳 */
  ts: number
  /** 模型名（文件名段） */
  model: string
  /** messages 字节 hash（前缀缓存诊断用） */
  msgs_hash: string
  msg_count: number
  /** 文件大小（字节） */
  size?: number
}

/** payload 快照内容响应 */
export interface PayloadDiagFile {
  name: string
  /** 原始请求体 JSON 字符串（含 model/messages/参数） */
  content: string
  error?: string
}

/**
 * 获取 payload 快照列表（时间倒序）
 */
export async function getPayloadDiagList(): Promise<{ items: PayloadDiagItem[]; total: number }> {
  const response = await apiClient.get<{ items: PayloadDiagItem[]; total: number }>(
    MONITORING_ENDPOINTS.mon_payload_diag_list,
  )
  return response.data
}

/**
 * 读取单个 payload 快照的完整请求体
 */
export async function getPayloadDiagFile(name: string): Promise<PayloadDiagFile> {
  const response = await apiClient.get<PayloadDiagFile>(MONITORING_ENDPOINTS.mon_payload_diag_get, {
    params: { name },
  })
  return response.data
}
