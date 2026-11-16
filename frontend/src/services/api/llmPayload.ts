/**
 * LLM Payload 诊断 API 服务
 *
 * 数据源：monitoring 插件 payload-diag（快照由 llm adapter 在
 * 每次 LLM 请求前落盘，含真实发送的 model + messages 请求体）。
 * 开关：用户空间 settings.yaml 的 diagnostics 段（默认开启），本文件下方
 * 的 diagnostics-settings API 即其读写面（env 开关已退役，ADR
 * 2026-10-10-diagnostics-settings-yaml）。
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

/** 诊断开关集合（用户空间 settings.yaml diagnostics 段，键面单点登记于 SDK DEFAULTS） */
export interface DiagnosticsSettings {
  payload_diag: boolean
  prompt_audit: boolean
}

/**
 * 读取诊断开关（无 settings.yaml 时后端回落全默认）
 */
export async function getDiagnosticsSettings(): Promise<{ diagnostics: DiagnosticsSettings }> {
  const response = await apiClient.get<{ diagnostics: DiagnosticsSettings }>(
    MONITORING_ENDPOINTS.mon_diagnostics_settings_get,
  )
  return response.data
}

/**
 * 更新诊断开关（合并写 settings.yaml；热生效——下一次 LLM 调用即按新值）
 */
export async function updateDiagnosticsSettings(
  partial: Partial<DiagnosticsSettings>,
): Promise<{ diagnostics: DiagnosticsSettings }> {
  const response = await apiClient.put<{ diagnostics: DiagnosticsSettings }>(
    MONITORING_ENDPOINTS.mon_diagnostics_settings_put,
    partial,
  )
  return response.data
}
