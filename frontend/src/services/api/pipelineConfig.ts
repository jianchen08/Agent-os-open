/**
 * 管道配置 API 服务
 *
 * 对接内核 P7 端点（kernel/crates/api/src/routes.rs §P7）：
 * - GET /api/v1/config/pipelines/{name}：读管道配置（config/pipelines/{name}.yaml → JSON），
 *   返回 { name, data, etag }
 * - PUT /api/v1/config/pipelines/{name}：原子写回管道配置，body { data, if_match }，
 *   返回 { name, etag }；内核写盘前做 G10 文件 DSL 结构校验（旧形态键/死形态拒绝）
 *
 * 注意：内核 config_service 的 denylist 含 "pipelines"，通用 generic config 接口不暴露管道配置，
 * 必须走本服务的专用端点（见 config_service.rs B1 denylist）。
 *
 * 本服务只做"连接 + 数据"层——不渲染表单、不做布局。
 *
 * @module api/pipelineConfig
 */

import { API_ENDPOINTS } from '@/constants/api'
import apiClient from '@/services/api/client'
import { requestWithRetry } from '@/utils/retry'
import type { RetryOptions } from '@/utils/retry'

/** GET 管道配置响应体（与后端 PipelineConfigResponse 对齐） */
export interface PipelineConfigResponse {
  name: string
  /** 管道配置内容（YAML 解析后的 JSON，G10 文件 DSL：loop_bodies/next/while） */
  data: Record<string, unknown>
  /** 内容 ETag（B4 乐观锁语义，PUT 时回传 if_match） */
  etag: string
}

/** PUT 保存管道配置的返回体 */
export interface PipelineConfigSaveResult {
  name: string
  etag: string
}

/**
 * GET 读取管道配置。
 *
 * @param name - 管道名（对应 config/pipelines/{name}.yaml，如 default / l1-main）
 * @param options - 重试选项
 * @returns { name, data, etag }
 */
export async function getPipelineConfig(
  name: string,
  options: RetryOptions = {},
): Promise<PipelineConfigResponse> {
  return requestWithRetry(async () => {
    const response = await apiClient.get<PipelineConfigResponse>(
      API_ENDPOINTS.CONFIG.PIPELINE_GET(name),
    )
    return response.data
  }, options)
}

/**
 * PUT 保存管道配置。
 *
 * @param name - 管道名
 * @param data - 完整管道配置内容（与 GET 返回的 data 同构，G10 文件 DSL）
 * @param ifMatch - GET 返回的 ETag（If-Match 乐观锁；缺失/不匹配内核 409）
 * @param options - 重试选项
 * @returns { name, etag }（新 ETag）
 */
export async function savePipelineConfig(
  name: string,
  data: Record<string, unknown>,
  ifMatch: string,
  options: RetryOptions = {},
): Promise<PipelineConfigSaveResult> {
  return requestWithRetry(async () => {
    const response = await apiClient.put<PipelineConfigSaveResult>(
      API_ENDPOINTS.CONFIG.PIPELINE_UPDATE(name),
      { data, if_match: ifMatch },
    )
    return response.data
  }, options)
}
