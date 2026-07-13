/**
 * 批注 API 调用
 *
 * 封装批注相关的 REST API 请求。
 */

import { apiClient } from './client'

const ARTIFACTS_BASE = '/api/v1/artifacts'
const ANNOTATIONS_BASE = '/api/v1/annotations'

/** 获取制品的批注列表 */
export async function listAnnotations(artifactId: string, status?: string, limit = 100): Promise<any> {
  let url = `${ARTIFACTS_BASE}/${artifactId}/annotations?limit=${limit}`
  if (status) url += `&status=${encodeURIComponent(status)}`
  return apiClient.get(url)
}

/** 添加批注 */
export async function createAnnotation(artifactId: string, params: {
  targetType: string
  targetData: Record<string, any>
  content: string
  authorType?: string
  authorId?: string
}): Promise<any> {
  return apiClient.post(`${ARTIFACTS_BASE}/${artifactId}/annotations`, {
    target_type: params.targetType,
    target_data: params.targetData,
    content: params.content,
    author_type: params.authorType ?? 'user',
    author_id: params.authorId ?? '',
  })
}

/** 更新批注 */
export async function updateAnnotation(annotationId: string, params: {
  content?: string
  targetData?: Record<string, any>
}): Promise<any> {
  return apiClient.put(`${ANNOTATIONS_BASE}/${annotationId}`, {
    content: params.content,
    target_data: params.targetData,
  })
}

/** 删除批注 */
export async function deleteAnnotation(annotationId: string): Promise<any> {
  return apiClient.delete(`${ANNOTATIONS_BASE}/${annotationId}`)
}

/** 标记批注为已解决 */
export async function resolveAnnotation(annotationId: string): Promise<any> {
  return apiClient.post(`${ANNOTATIONS_BASE}/${annotationId}/resolve`, {})
}
