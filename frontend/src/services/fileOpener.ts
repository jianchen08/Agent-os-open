/**
 * 文件打开服务
 *
 * 提供统一的文件打开入口，根据文件后缀从后端配置解析编辑器类型，
 * 然后路由到对应的打开方式（外部IDE / 内置编辑器 / 系统默认应用）。
 *
 * 暴露接口：
 * - openFile(filePath, options) - 打开文件的统一入口
 * - EditorType - 编辑器类型枚举
 * - setBuiltinOpenHandler(handler) - 设置内置编辑器打开处理器
 */

import { resolveEditor } from './api/editorConfig'
import { openFileInIDE } from './api/workspaces'
import { apiClient } from './api/client'
import { registerFileEditor } from '@/stores/fileEditorRegistry'
import { useLayoutModeStore } from '@/stores/layoutModeStore'

/** 编辑器类型 */
export type EditorType = 'ide' | 'builtin' | 'external'

/** 内置编辑器打开处理器 */
let builtinOpenHandler: ((filePath: string, line?: number, column?: number) => Promise<void>) | null = null

/**
 * 设置内置编辑器打开处理器
 *
 * @param handler - 内置编辑器打开处理函数
 */
export function setBuiltinOpenHandler(handler: (filePath: string, line?: number, column?: number) => Promise<void>): void {
  builtinOpenHandler = handler
}

/**
 * 打开文件的统一入口
 *
 * 根据文件后缀从后端配置解析编辑器类型，然后路由到对应的打开方式：
 * - ide: 通过连接器在外部IDE中打开
 * - builtin: 通过回调在内置编辑器中打开
 * - external: 通过后端API用系统默认应用打开
 *
 * 具备降级机制：当 IDE 不可用或解析失败时，自动降级到内置编辑器。
 *
 * @param filePath - 文件路径，支持绝对路径（如 'd:/project/src/index.ts'）或相对路径
 * @param options - 可选参数
 * @param options.line - 跳转到的行号
 * @param options.column - 跳转到的列号
 * @returns 打开结果，包含 success、editor 和可选的 message
 */
export async function openFile(
  filePath: string,
  options?: {
    line?: number
    column?: number
  },
): Promise<{ success: boolean; editor: EditorType; message?: string }> {
  try {
    const resp = await resolveEditor(filePath)
    const { editor } = resp.data
    const editorType = editor as EditorType

    switch (editorType) {
      case 'ide': {
        const result = await openFileInIDE(filePath, options?.line, options?.column)
        const data = result.data
        if (!data.success) {
          // IDE连接器不可用，降级到内置编辑器
          if (builtinOpenHandler) {
            await builtinOpenHandler(filePath, options?.line, options?.column)
          }
          return { success: true, editor: 'builtin', message: 'IDE不可用，已切换到内置编辑器' }
        }
        return { success: true, editor: 'ide' }
      }
      case 'builtin': {
        if (builtinOpenHandler) {
          await builtinOpenHandler(filePath, options?.line, options?.column)
        }
        return { success: true, editor: 'builtin' }
      }
      case 'external': {
        // 外部应用通过后端API打开（后续可扩展）
        if (builtinOpenHandler) {
          await builtinOpenHandler(filePath, options?.line, options?.column)
        }
        return { success: true, editor: 'external', message: '暂不支持外部应用，已使用内置编辑器' }
      }
      default: {
        if (builtinOpenHandler) {
          await builtinOpenHandler(filePath, options?.line, options?.column)
        }
        return { success: true, editor: 'builtin' }
      }
    }
  } catch {
    // 解析失败，降级到内置编辑器
    if (builtinOpenHandler) {
      await builtinOpenHandler(filePath, options?.line, options?.column)
    }
    return { success: true, editor: 'builtin', message: '解析失败，已使用内置编辑器' }
  }
}

/**
 * 默认的内置编辑器打开处理函数
 *
 * 使用 _local 工作空间接口直接读取本地文件内容，然后在内置编辑器中打开。
 *
 * @param filePath - 文件路径（绝对路径）
 * @param line - 行号（可选）
 * @param column - 列号（可选）
 */
async function defaultBuiltinOpenHandler(
  filePath: string,
  line?: number,
  column?: number,
): Promise<void> {
  const tabId = `file-local-${filePath.replace(/[/\\]/g, '_')}`
  const layoutStore = useLayoutModeStore.getState()

  // 如果 Tab 已存在，直接激活
  const existingTab = layoutStore.workspaceTabs.find(t => t.id === tabId)
  if (existingTab) {
    layoutStore.setActiveTab(tabId)
    return
  }

  try {
    // 使用 _local 工作空间读取本地文件
    const resp = await apiClient.get('/api/v1/workspaces/_local/file-content', {
      params: { path: filePath }
    })
    if (resp.data?.success) {
      const fileName = filePath.split(/[/\\]/).pop() || filePath
      registerFileEditor(tabId, {
        filePath,
        fileName,
        content: resp.data.content ?? '',
        size: resp.data.size,
        containerTaskId: '_local',
      })
      layoutStore.addWorkspaceTab({
        id: tabId,
        title: fileName,
        icon: '📄',
        moduleId: '__file_editor__',
        isActive: true,
        isPinned: false,
      })
    } else {
      console.warn('[fileOpener] 读取文件失败:', resp.data?.message)
    }
  } catch (error) {
    console.error('[fileOpener] 打开文件失败:', error)
  }
}

// 初始化时设置默认处理器
setBuiltinOpenHandler(defaultBuiltinOpenHandler)
