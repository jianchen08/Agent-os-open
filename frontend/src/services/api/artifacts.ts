/**
 * 制品 API 调用
 *
 * 封装制品相关的 REST API 请求。
 */

import { apiClient } from './client'

const BASE = '/api/v1/artifacts'

/** 创建制品 */
export async function createArtifact(params: {
  taskId: string
  title: string
  artifactType: string
  content?: string
  filePath?: string
  metadata?: Record<string, any>
}): Promise<any> {
  return apiClient.post(BASE, {
    task_id: params.taskId,
    title: params.title,
    artifact_type: params.artifactType,
    content: params.content ?? '',
    file_path: params.filePath,
    metadata: params.metadata,
  })
}

/** 获取制品详情 */
export async function getArtifact(artifactId: string): Promise<any> {
  return apiClient.get(`${BASE}/${artifactId}`)
}

/** 获取任务下的制品列表 */
export async function listArtifactsByTask(taskId: string, limit = 50, offset = 0): Promise<any> {
  return apiClient.get(`${BASE}?task_id=${encodeURIComponent(taskId)}&limit=${limit}&offset=${offset}`)
}

/** 更新制品（创建新版本） */
export async function updateArtifact(artifactId: string, params: {
  content?: string
  title?: string
  metadata?: Record<string, any>
}): Promise<any> {
  return apiClient.put(`${BASE}/${artifactId}`, {
    content: params.content,
    title: params.title,
    metadata: params.metadata,
  })
}

/** 删除制品 */
export async function deleteArtifact(artifactId: string): Promise<any> {
  return apiClient.delete(`${BASE}/${artifactId}`)
}

/** 获取版本历史 */
export async function getVersionHistory(artifactId: string): Promise<any> {
  return apiClient.get(`${BASE}/${artifactId}/versions`)
}

/** 获取版本差异 */
export async function getVersionDiff(artifactId: string, fromVersion: number, toVersion: number): Promise<any> {
  return apiClient.get(`${BASE}/${artifactId}/diff?from=${fromVersion}&to=${toVersion}`)
}
