/**
 * 编辑器配置 Store
 *
 * 管理编辑器配置的状态，包括后缀到编辑器类型的映射、
 * 默认编辑器和编辑器定义。提供根据文件路径获取编辑器类型的能力。
 *
 * 暴露接口：
 * - useEditorConfigStore - Zustand store hook
 * - fetchConfig() - 从后端加载编辑器配置
 * - getEditorForFile(filePath) - 根据文件路径获取编辑器类型
 */

import { create } from 'zustand'
import { getEditorConfig } from '@/services/api/editorConfig'

interface EditorConfigState {
  /** 编辑器配置映射（后缀 → 编辑器类型），如 { '.ts': 'ide', '.md': 'builtin' } */
  mappings: Record<string, string>
  /** 默认编辑器类型 */
  defaultEditor: string
  /** 编辑器定义信息 */
  editors: Record<string, any>
  /** 配置是否已从后端加载 */
  loaded: boolean
}

interface EditorConfigActions {
  /**
   * 从后端加载编辑器配置
   *
   * 获取配置后更新 store 状态，失败时静默处理（使用默认配置）。
   */
  fetchConfig: () => Promise<void>
  /**
   * 根据文件路径获取对应的编辑器类型
   *
   * 通过文件扩展名在 mappings 中查找对应的编辑器类型，
   * 未找到时返回 defaultEditor。
   *
   * @param filePath - 文件路径，如 '/project/src/index.ts'
   * @returns 编辑器类型，如 'ide'、'builtin'、'external'
   */
  getEditorForFile: (filePath: string) => string
}

export const useEditorConfigStore = create<EditorConfigState & EditorConfigActions>()(
  (set, get) => ({
    mappings: {},
    defaultEditor: 'builtin',
    editors: {},
    loaded: false,

    fetchConfig: async () => {
      try {
        const resp = await getEditorConfig()
        const data = resp.data
        set({
          mappings: data.mappings || {},
          defaultEditor: data.default_editor || 'builtin',
          editors: data.editors || {},
          loaded: true,
        })
      } catch {
        // 静默失败，使用默认配置
      }
    },

    getEditorForFile: (filePath: string) => {
      const { mappings, defaultEditor } = get()
      const ext = filePath.includes('.') ? '.' + filePath.split('.').pop()?.toLowerCase() : ''
      return mappings[ext] || defaultEditor
    },
  }),
)
