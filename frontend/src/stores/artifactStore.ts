/**
 * Artifact Store
 *
 * 管理制品的加载、缓存和版本追踪。
 */

import { create } from 'zustand'
import type { Artifact } from '@/types/artifact'

interface ArtifactState {
  /** 以 artifact_id 为 key 的制品缓存 */
  artifacts: Record<string, Artifact>
  /** 制品版本历史缓存 */
  versionHistories: Record<string, Artifact[]>
  /** 加载状态 */
  loading: boolean
  /** 错误信息 */
  error: string | null
}

interface ArtifactActions {
  /** 加载单个制品 */
  fetchArtifact: (artifactId: string) => Promise<Artifact | null>
  /** 加载任务下的所有制品 */
  fetchArtifactsByTask: (taskId: string) => Promise<Artifact[]>
  /** 加载版本历史 */
  fetchVersionHistory: (artifactId: string) => Promise<Artifact[]>
  /** 加载版本差异 */
  fetchVersionDiff: (artifactId: string, from: number, to: number) => Promise<string>
  /** 处理 WebSocket artifact_updated 事件 */
  updateArtifactFromWS: (event: { artifact_id: string; old_artifact_id?: string; version?: number; [key: string]: any }) => void
  /** 处理 WebSocket artifact_created 事件 */
  addArtifactFromWS: (event: { artifact_id: string; task_id?: string; artifact_type?: string; title?: string; [key: string]: any }) => void
  /** 清除缓存 */
  clearCache: () => void
}

const API_BASE = '/api/v1/artifacts'

export const useArtifactStore = create<ArtifactState & ArtifactActions>()((set, get) => ({
  artifacts: {},
  versionHistories: {},
  loading: false,
  error: null,

  fetchArtifact: async (artifactId) => {
    set({ loading: true, error: null })
    try {
      const resp = await fetch(`${API_BASE}/${artifactId}`)
      const data = await resp.json()
      if (data.error) {
        set({ loading: false, error: data.error.message })
        return null
      }
      const artifact = _normalizeArtifact(data)
      set((state) => ({
        artifacts: { ...state.artifacts, [artifact.id]: artifact },
        loading: false,
      }))
      return artifact
    } catch (e: any) {
      set({ loading: false, error: e.message })
      return null
    }
  },

  fetchArtifactsByTask: async (taskId) => {
    set({ loading: true, error: null })
    try {
      const resp = await fetch(`${API_BASE}?task_id=${encodeURIComponent(taskId)}`)
      const data = await resp.json()
      const items = (data.items || []).map(_normalizeArtifact)
      set((state) => {
        const updated = { ...state.artifacts }
        for (const a of items) {
          updated[a.id] = a
        }
        return { artifacts: updated, loading: false }
      })
      return items
    } catch (e: any) {
      set({ loading: false, error: e.message })
      return []
    }
  },

  fetchVersionHistory: async (artifactId) => {
    set({ loading: true, error: null })
    try {
      const resp = await fetch(`${API_BASE}/${artifactId}/versions`)
      const data = await resp.json()
      const items = (data.items || []).map(_normalizeArtifact)
      set((state) => ({
        versionHistories: { ...state.versionHistories, [artifactId]: items },
        loading: false,
      }))
      // 更新缓存中的制品
      set((state) => {
        const updated = { ...state.artifacts }
        for (const a of items) {
          updated[a.id] = a
        }
        return { artifacts: updated }
      })
      return items
    } catch (e: any) {
      set({ loading: false, error: e.message })
      return []
    }
  },

  fetchVersionDiff: async (artifactId, from, to) => {
    try {
      const resp = await fetch(`${API_BASE}/${artifactId}/diff?from=${from}&to=${to}`)
      const data = await resp.json()
      return data.diff || ''
    } catch {
      return ''
    }
  },

  updateArtifactFromWS: (event) => {
    if (event.artifact_id) {
      // 标记需要刷新
      set((state) => {
        const existing = state.artifacts[event.artifact_id]
        if (existing) {
          return {
            artifacts: {
              ...state.artifacts,
              [event.artifact_id]: {
                ...existing,
                version: event.version ?? existing.version + 1,
              },
            },
          }
        }
        return state
      })
    }
  },

  addArtifactFromWS: (event) => {
    if (event.artifact_id) {
      const artifact: Artifact = {
        id: event.artifact_id,
        taskId: event.task_id ?? '',
        title: event.title ?? '',
        artifactType: (event.artifact_type as any) ?? 'text',
        content: '',
        version: 1,
        metadata: {},
        createdAt: new Date().toISOString(),
        updatedAt: new Date().toISOString(),
      }
      set((state) => ({
        artifacts: { ...state.artifacts, [artifact.id]: artifact },
      }))
    }
  },

  clearCache: () => {
    set({ artifacts: {}, versionHistories: {}, error: null })
  },
}))

/** 将后端 API 响应转换为前端 Artifact 类型 */
function _normalizeArtifact(data: Record<string, any>): Artifact {
  if (!data.id) {
    console.error('[artifactStore] _normalizeArtifact: id 字段缺失', data)
  }
  return {
    id: data.id ?? '',
    taskId: data.taskId ?? '',
    title: data.title ?? '',
    artifactType: data.artifactType ?? 'text',
    content: data.content ?? '',
    filePath: data.filePath,
    version: data.version ?? 1,
    parentArtifactId: data.parentArtifactId,
    metadata: data.metadata ?? {},
    createdAt: data.createdAt ?? '',
    updatedAt: data.updatedAt ?? '',
  }
}
