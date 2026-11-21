import { useEffect } from 'react'
import apiClient from '@/services/api/client'
import { WORKSPACE_SERVICE_ENDPOINTS as W } from '@/services/api/endpoints.generated'
import { EDITABLE_FILE_MAX_BYTES } from '@/services/fileOpener'
import {
  emitFileChange,
  getFileEditorData,
  updateFileEditorData,
} from '@/stores/fileEditorRegistry'
import { useLayoutModeStore } from '@/stores/layoutModeStore'

/** 文件编辑器自动刷新逻辑 每 3 秒轮询检查已打开的文件编辑器 Tab 对应的文件是否被外部修改， */
export function useFileEditorAutoRefresh(): void {
  useEffect(() => {
    const intervalMs = 3000
    const timer = setInterval(async () => {
      const tabs = useLayoutModeStore.getState().workspaceTabs
      const fileEditorTabs = tabs.filter(
        (t) => t.moduleId === '__file_editor__' && t.isActive
      )

      for (const tab of fileEditorTabs) {
        const editorData = getFileEditorData(tab.id)
        if (!editorData || !editorData.containerTaskId) continue
        // 部分加载（按需加载未读完）与大只读文件跳过外部修改轮询：轮询全量
        // 拉取会冲掉分页状态、造成周期性大流量；轮询只为防编辑冲突，
        // partial 禁编辑、超阈值只读，均无冲突面（阈值与 CodeEditor 同源）
        const isPartial =
          editorData.loadedBytes !== undefined &&
          editorData.size !== undefined &&
          editorData.loadedBytes < editorData.size
        if (isPartial || (editorData.size ?? 0) > EDITABLE_FILE_MAX_BYTES) continue

        try {
          const resp = await apiClient.get(
            W.workspaces_file_content_get.replace('{container_task_id}', editorData.containerTaskId),
            { params: { path: editorData.filePath } }
          )
          if (resp.data?.success && resp.data.content !== undefined) {
            const newContent = resp.data.content
            const newSize = resp.data.size
            // 仅当内容真正变化时才更新
            if (newContent !== editorData.content) {
              updateFileEditorData(tab.id, {
                content: newContent,
                size: newSize,
              })
              emitFileChange(tab.id, newContent, newSize)
            }
          }
        } catch {
          // 静默失败，不影响用户体验
        }
      }
    }, intervalMs)

    return () => clearInterval(timer)
  }, [])
}
