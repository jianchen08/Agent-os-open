/** 文件打开服务 提供统一的文件打开入口，根据文件后缀从后端配置解析编辑器类型， */

import { resolveEditor } from './api/editorConfig'
import { openFileInIDE } from './api/workspaces'
import { apiClient } from './api/client'
import { registerFileEditor } from '@/stores/fileEditorRegistry'
import { useLayoutModeStore } from '@/stores/layoutModeStore'

/** 编辑器类型 */
export type EditorType = 'ide' | 'builtin' | 'external'

/** 内置编辑器打开处理器 */
let builtinOpenHandler: ((filePath: string, line?: number, column?: number, containerTaskId?: string) => Promise<void>) | null = null

/** 设置内置编辑器打开处理器 */
export function setBuiltinOpenHandler(
  handler: (filePath: string, line?: number, column?: number, containerTaskId?: string) => Promise<void>,
): void {
  builtinOpenHandler = handler
}

/** 打开文件的统一入口 根据文件后缀从后端配置解析编辑器类型，然后路由到对应的打开方式： */
export async function openFile(
  filePath: string,
  options?: {
    line?: number
    column?: number
    containerTaskId?: string
  },
): Promise<{ success: boolean; editor: EditorType; message?: string }> {
  try {
    // 404:
    const containerTaskId = options?.containerTaskId
    if (builtinOpenHandler) {
      await builtinOpenHandler(filePath, options?.line, options?.column, containerTaskId)
    }
    return { success: true, editor: 'builtin' }
  } catch {
    // 解析失败，降级到内置编辑器
    if (builtinOpenHandler) {
      await builtinOpenHandler(filePath, options?.line, options?.column, options?.containerTaskId)
    }
    return { success: true, editor: 'builtin', message: '解析失败，已使用内置编辑器' }
  }
}

/** 默认的内置编辑器打开处理函数 使用 _local 工作空间接口直接读取本地文件内容，然后在内置编辑器中打开。 */
async function defaultBuiltinOpenHandler(
  filePath: string,
  line?: number,
  column?: number,
  containerTaskId?: string,
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
    // 优先使用任务容器 ID，否则 fallback 到 _local（项目根目录）
    const resolvedContainerId = containerTaskId || '_local'
    let resp = await apiClient.get(`/api/v1/workspaces/${resolvedContainerId}/file-content`, {
      params: { path: filePath },
    })

    // 任务工作空间未找到文件时，回退到项目根目录 _local 重试
    if (!resp.data?.success && resolvedContainerId !== '_local') {
      resp = await apiClient.get('/api/v1/workspaces/_local/file-content', {
        params: { path: filePath },
      })
    }

    if (resp.data?.success) {
      const fileName = filePath.split(/[/\\]/).pop() || filePath
      registerFileEditor(tabId, {
        filePath,
        fileName,
        content: resp.data.content ?? '',
        size: resp.data.size,
        containerTaskId: resolvedContainerId,
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
