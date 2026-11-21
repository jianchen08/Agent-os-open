/**
 * 批注 API 封装（任务现场批 5）
 *
 * 后端 5 个端点已就绪（`plugins/shared/artifacts/plugin.json` 的
 * `artifacts_annotations_list/create` + `annotations_update/delete/resolve`），
 * 但前端此前**零调用方**（`endpoints.generated.ts` 有端点表、无消费）——本文件
 * 是唯一消费入口（方案 §3.6.3）。
 *
 * ⚠️ 落点如实交代：批注当前是 artifacts 插件的**纯内存存储**
 * （`plugins/shared/artifacts/annotation_service.py` 头部自述、`:37-45` 为 dict），
 * **内核/插件进程重启即丢**。故 UI 必须标注「本次会话内批注」，不得暗示已持久化
 * （方案 §3.6.4 一期口径；持久化=插件侧落盘，列二期）。
 */

import { apiClient } from './client'
import { ARTIFACTS_ENDPOINTS as A } from './endpoints.generated'
import type { Annotation, AnnotationTargetData, AnnotationTarget } from '@/types/artifact'

/** 新建批注入参（后端 create 端点字段口径） */
export interface CreateAnnotationInput {
  /** 批注对象类型（文本选区 / 整篇） */
  targetType: AnnotationTarget
  /** 目标数据（文本选区用 startOffset/endOffset/selectedText） */
  targetData: AnnotationTargetData
  /** 批注内容 */
  content: string
  /** 作者类型：用户手写=user；机器生成=agent（UI 只读展示） */
  authorType?: 'user' | 'agent'
  /** 作者标识（缺省前端用户标识，后端可覆写） */
  authorId?: string
}

/** 批注列表响应（后端可能直接回数组或 {items}，两种都收） */
function normalizeList(payload: unknown): Annotation[] {
  if (Array.isArray(payload)) return payload as Annotation[]
  if (typeof payload === 'object' && payload !== null) {
    const items = (payload as { items?: unknown }).items
    if (Array.isArray(items)) return items as Annotation[]
  }
  return []
}

/** 列某产物的全部批注 */
export async function listAnnotations(artifactId: string): Promise<Annotation[]> {
  const response = await apiClient.get<unknown>(
    A.artifacts_annotations_list.replace('{artifact_id}', artifactId),
  )
  return normalizeList(response.data)
}

/** 新建批注（返回后端落定的批注实体） */
export async function createAnnotation(
  artifactId: string,
  input: CreateAnnotationInput,
): Promise<Annotation> {
  const response = await apiClient.post<Annotation>(
    A.artifacts_annotations_create.replace('{artifact_id}', artifactId),
    {
      target_type: input.targetType,
      target_data: input.targetData,
      content: input.content,
      author_type: input.authorType ?? 'user',
      ...(input.authorId ? { author_id: input.authorId } : {}),
    },
  )
  return response.data
}

/** 修改批注内容（仅 user 作者在 UI 上开放） */
export async function updateAnnotation(annotationId: string, content: string): Promise<Annotation> {
  const response = await apiClient.put<Annotation>(
    A.annotations_update.replace('{annotation_id}', annotationId),
    { content },
  )
  return response.data
}

/** 删除批注 */
export async function deleteAnnotation(annotationId: string): Promise<void> {
  await apiClient.delete(A.annotations_delete.replace('{annotation_id}', annotationId))
}

/** 标记已解决 / 重新打开 */
export async function resolveAnnotation(annotationId: string, resolved = true): Promise<Annotation> {
  const response = await apiClient.post<Annotation>(
    A.annotations_resolve.replace('{annotation_id}', annotationId),
    { resolved },
  )
  return response.data
}

/**
 * 批注导出（Markdown，前端生成——后端无导出端点，方案 §3.6.4）。
 *
 * 输出每条批注：序号 + 作者类型 + 目标摘要 + 内容 + 状态。文本选区批注带上
 * 字符偏移，便于外部工具回指原文位置。
 */
export function annotationsToMarkdown(
  annotations: readonly Annotation[],
  options?: { documentName?: string; artifactId?: string },
): string {
  const header = [
    `# 批注导出${options?.documentName ? ` · ${options.documentName}` : ''}`,
    '',
    ...(options?.artifactId ? [`- 产物 ID: ${options.artifactId}`] : []),
    `- 批注数量: ${annotations.length}`,
    '- 存储说明: 当前为会话内内存态（重启丢失）',
    '',
  ].join('\n')

  const body = annotations
    .map((annotation, index) => {
      const target = describeTarget(annotation)
      const status = annotation.status === 'resolved' ? '（已解决）' : ''
      const author = annotation.authorType === 'agent' ? '机器' : '用户'
      return [`## ${index + 1}. ${author}${status}`, '', ...(target ? [`> ${target}`, ''] : []), annotation.content, ''].join('\n')
    })
    .join('\n')

  return `${header}${body}`.trimEnd() + '\n'
}

/** 批注目标的一行摘要（导出与侧栏共用） */
export function describeTarget(annotation: Annotation): string {
  const data = annotation.targetData
  if (annotation.targetType === 'text_selection' && typeof data === 'object' && data !== null) {
    const selection = data as { selectedText?: unknown; startOffset?: unknown; endOffset?: unknown }
    const text = typeof selection.selectedText === 'string' ? selection.selectedText : ''
    const start = typeof selection.startOffset === 'number' ? selection.startOffset : undefined
    const end = typeof selection.endOffset === 'number' ? selection.endOffset : undefined
    const range = start !== undefined && end !== undefined ? `[${start}-${end}]` : ''
    return text ? `${range} 「${text.length > 40 ? `${text.slice(0, 40)}…` : text}」` : range
  }
  if (annotation.targetType === 'whole_artifact') return '整篇文档'
  return ''
}
