/**
 * 场景管理 Store
 *
 * 使用 Zustand 管理场景状态，包括场景列表、活跃场景、模板等。
 * 支持场景的创建、切换、删除操作。
 *
 * @module stores/sceneStore
 */

import { create } from 'zustand'
import {
  createScene,
  deleteScene,
  getScene,
  getSceneTemplates,
  listScenes,
  switchScene,
  updateScene,
} from '@/services/api/scenes'
import type {
  CreateSceneRequest,
  Scene,
  SceneTemplate,
  UpdateSceneRequest,
} from '@/services/api/scenes'

/**
 * 场景 Store 状态接口
 */
interface SceneState {
  /** 场景列表 */
  scenes: Scene[]
  /** 当前活跃场景 ID */
  activeSceneId: string | null
  /** 场景模板列表 */
  templates: SceneTemplate[]
  /** 加载状态 */
  isLoading: boolean
  /** 错误信息 */
  error: string | null

  // ---- Actions ----

  /** 获取场景列表 */
  fetchScenes: () => Promise<void>
  /** 获取场景详情 */
  fetchScene: (sceneId: string) => Promise<Scene | null>
  /** 创建场景 */
  createScene: (request: CreateSceneRequest) => Promise<Scene | null>
  /** 更新场景 */
  updateScene: (sceneId: string, request: UpdateSceneRequest) => Promise<Scene | null>
  /** 删除场景 */
  deleteScene: (sceneId: string) => Promise<boolean>
  /** 切换活跃场景 */
  switchScene: (sceneId: string) => Promise<Scene | null>
  /** 获取模板列表 */
  fetchTemplates: () => Promise<void>
  /** 清除错误 */
  clearError: () => void
}

/**
 * 场景管理 Store
 *
 * 管理场景列表、活跃场景和模板数据。
 * 自动处理活跃场景的切换状态。
 */
export const useSceneStore = create<SceneState>((set, get) => ({
  scenes: [],
  activeSceneId: null,
  templates: [],
  isLoading: false,
  error: null,

  /**
   * 获取场景列表
   */
  fetchScenes: async () => {
    const state = get()
    if (state.isLoading) return

    set({ isLoading: true, error: null })
    try {
      const response = await listScenes()
      const scenes = response.items || []

      // 从列表中推断活跃场景
      const activeScene = scenes.find((s) => s.is_active)
      set({
        scenes,
        activeSceneId: activeScene?.id ?? null,
        isLoading: false,
      })
    } catch (err) {
      const message = err instanceof Error ? err.message : '获取场景列表失败'
      set({ isLoading: false, error: message })
    }
  },

  /**
   * 获取场景详情
   */
  fetchScene: async (sceneId: string) => {
    try {
      const scene = await getScene(sceneId)
      // 更新列表中的场景数据
      set((state) => ({
        scenes: state.scenes.map((s) => (s.id === sceneId ? scene : s)),
      }))
      return scene
    } catch (err) {
      const message = err instanceof Error ? err.message : '获取场景失败'
      set({ error: message })
      return null
    }
  },

  /**
   * 创建场景
   */
  createScene: async (request: CreateSceneRequest) => {
    set({ isLoading: true, error: null })
    try {
      const scene = await createScene(request)
      set((state) => ({
        scenes: [...state.scenes, scene],
        isLoading: false,
      }))
      return scene
    } catch (err) {
      const message = err instanceof Error ? err.message : '创建场景失败'
      set({ isLoading: false, error: message })
      return null
    }
  },

  /**
   * 更新场景
   */
  updateScene: async (sceneId: string, request: UpdateSceneRequest) => {
    try {
      const scene = await updateScene(sceneId, request)
      set((state) => ({
        scenes: state.scenes.map((s) => (s.id === sceneId ? scene : s)),
      }))
      return scene
    } catch (err) {
      const message = err instanceof Error ? err.message : '更新场景失败'
      set({ error: message })
      return null
    }
  },

  /**
   * 删除场景
   */
  deleteScene: async (sceneId: string) => {
    try {
      await deleteScene(sceneId)
      set((state) => {
        const newScenes = state.scenes.filter((s) => s.id !== sceneId)
        const newActiveId =
          state.activeSceneId === sceneId ? null : state.activeSceneId
        return { scenes: newScenes, activeSceneId: newActiveId }
      })
      return true
    } catch (err) {
      const message = err instanceof Error ? err.message : '删除场景失败'
      set({ error: message })
      return false
    }
  },

  /**
   * 切换活跃场景
   */
  switchScene: async (sceneId: string) => {
    set({ isLoading: true, error: null })
    try {
      const scene = await switchScene(sceneId)
      set((state) => ({
        scenes: state.scenes.map((s) => ({
          ...s,
          is_active: s.id === sceneId,
        })),
        activeSceneId: sceneId,
        isLoading: false,
      }))
      return scene
    } catch (err) {
      const message = err instanceof Error ? err.message : '切换场景失败'
      set({ isLoading: false, error: message })
      return null
    }
  },

  /**
   * 获取模板列表
   */
  fetchTemplates: async () => {
    try {
      const response = await getSceneTemplates()
      set({ templates: response.items || [] })
    } catch (err) {
      const message = err instanceof Error ? err.message : '获取模板列表失败'
      set({ error: message })
    }
  },

  /**
   * 清除错误
   */
  clearError: () => {
    set({ error: null })
  },
}))
