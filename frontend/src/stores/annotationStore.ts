/**
 * Annotation Store
 *
 * 管理制品批注的创建、展示和解决。
 */

import { create } from 'zustand'
import type { Annotation, AnnotationTarget, AnnotationStatus } from '@/types/artifact'

interface AnnotationState {
  /** 外层 key: artifact_id, 内层 key: annotation_id */
  annotations: Record<string, Record<string, Annotation>>
  /** 当前选中/编辑中的批注 ID */
  activeAnnotationId: string | null
  /** 当前批注模式 */
  annotationMode: 'none' | 'text' | 'image' | 'video'
}

interface AnnotationActions {
  /** 加载制品批注 */
  fetchAnnotations: (artifactId: string) => Promise<Annotation[]>
  /** 创建批注 */
  createAnnotation: (artifactId: string, annotation: {
    targetType: AnnotationTarget
    targetData: Record<string, any>
    content: string
    authorType?: string
    authorId?: string
  }) => Promise<Annotation | null>
  /** 更新批注 */
  updateAnnotation: (annotationId: string, updates: {
    content?: string
    targetData?: Record<string, any>
  }) => Promise<boolean>
  /** 删除批注 */
  deleteAnnotation: (annotationId: string) => Promise<boolean>
  /** 标记已解决 */
  resolveAnnotation: (annotationId: string) => Promise<boolean>
  /** 选中批注 */
  setActiveAnnotation: (annotationId: string | null) => void
  /** 切换批注模式 */
  setAnnotationMode: (mode: 'none' | 'text' | 'image' | 'video') => void
  /** 获取制品的批注列表 */
  getAnnotationsForArtifact: (artifactId: string) => Annotation[]
  /** 获取制品的活跃批注 */
  getActiveAnnotationsForArtifact: (artifactId: string) => Annotation[]
  /** 清除缓存 */
  clearCache: () => void
}

const API_BASE = '/api/v1/artifacts'

export const useAnnotationStore = create<AnnotationState & AnnotationActions>()((set, get) => ({
  annotations: {},
  activeAnnotationId: null,
  annotationMode: 'none',

  fetchAnnotations: async (artifactId) => {
    try {
      const resp = await fetch(`${API_BASE}/${artifactId}/annotations`)
      const data = await resp.json()
      const items = (data.items || []).map(_normalizeAnnotation)
      set((state) => {
        const artifactMap: Record<string, Annotation> = {}
        for (const a of items) {
          artifactMap[a.id] = a
        }
        return {
          annotations: {
            ...state.annotations,
            [artifactId]: { ...(state.annotations[artifactId] || {}), ...artifactMap },
          },
        }
      })
      return items
    } catch {
      return []
    }
  },

  createAnnotation: async (artifactId, annotation) => {
    try {
      const body: Record<string, any> = {
        target_type: annotation.targetType,
        target_data: annotation.targetData,
        content: annotation.content,
      }
      if (annotation.authorType) body.author_type = annotation.authorType
      if (annotation.authorId) body.author_id = annotation.authorId

      const resp = await fetch(`${API_BASE}/${artifactId}/annotations`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      const data = await resp.json()
      if (data.error) return null

      const ann = _normalizeAnnotation(data)
      set((state) => ({
        annotations: {
          ...state.annotations,
          [artifactId]: {
            ...(state.annotations[artifactId] || {}),
            [ann.id]: ann,
          },
        },
      }))
      return ann
    } catch {
      return null
    }
  },

  updateAnnotation: async (annotationId, updates) => {
    try {
      const body: Record<string, any> = {}
      if (updates.content !== undefined) body.content = updates.content
      if (updates.targetData !== undefined) body.target_data = updates.targetData

      const resp = await fetch(`/api/v1/annotations/${annotationId}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      const data = await resp.json()
      if (data.error) return false

      const ann = _normalizeAnnotation(data)
      // 更新缓存
      set((state) => {
        const updated = { ...state.annotations }
        for (const [artId, annMap] of Object.entries(updated)) {
          if (annMap[annotationId]) {
            updated[artId] = { ...annMap, [annotationId]: ann }
            break
          }
        }
        return { annotations: updated }
      })
      return true
    } catch {
      return false
    }
  },

  deleteAnnotation: async (annotationId) => {
    try {
      const resp = await fetch(`/api/v1/annotations/${annotationId}`, { method: 'DELETE' })
      const data = await resp.json()
      if (!data.success) return false

      set((state) => {
        const updated = { ...state.annotations }
        for (const [artId, annMap] of Object.entries(updated)) {
          if (annMap[annotationId]) {
            const { [annotationId]: _, ...rest } = annMap
            updated[artId] = rest
            break
          }
        }
        if (state.activeAnnotationId === annotationId) {
          return { annotations: updated, activeAnnotationId: null }
        }
        return { annotations: updated }
      })
      return true
    } catch {
      return false
    }
  },

  resolveAnnotation: async (annotationId) => {
    try {
      const resp = await fetch(`/api/v1/annotations/${annotationId}/resolve`, { method: 'POST' })
      const data = await resp.json()
      if (data.error) return false

      const ann = _normalizeAnnotation(data)
      set((state) => {
        const updated = { ...state.annotations }
        for (const [artId, annMap] of Object.entries(updated)) {
          if (annMap[annotationId]) {
            updated[artId] = { ...annMap, [annotationId]: ann }
            break
          }
        }
        return { annotations: updated }
      })
      return true
    } catch {
      return false
    }
  },

  setActiveAnnotation: (annotationId) => {
    set({ activeAnnotationId: annotationId })
  },

  setAnnotationMode: (mode) => {
    set({ annotationMode: mode })
  },

  getAnnotationsForArtifact: (artifactId) => {
    const annMap = get().annotations[artifactId]
    return annMap ? Object.values(annMap) : []
  },

  getActiveAnnotationsForArtifact: (artifactId) => {
    const annMap = get().annotations[artifactId]
    if (!annMap) return []
    return Object.values(annMap).filter((a) => a.status === 'active')
  },

  clearCache: () => {
    set({ annotations: {}, activeAnnotationId: null, annotationMode: 'none' })
  },
}))

/** 将后端 API 响应转换为前端 Annotation 类型 */
function _normalizeAnnotation(data: Record<string, any>): Annotation {
  if (!data.id) {
    console.error('[annotationStore] _normalizeAnnotation: id 字段缺失', data)
  }
  return {
    id: data.id ?? '',
    artifactId: data.artifactId ?? '',
    targetType: data.targetType ?? 'whole_artifact',
    targetData: data.targetData ?? {},
    content: data.content ?? '',
    authorType: data.authorType ?? 'user',
    authorId: data.authorId ?? '',
    status: data.status ?? 'active',
    createdAt: data.createdAt ?? '',
    resolvedAt: data.resolvedAt,
  }
}
