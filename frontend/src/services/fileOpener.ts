/** 文件打开服务 提供统一的文件打开入口，根据文件后缀从后端配置解析编辑器类型， */

import { WORKSPACE_SERVICE_ENDPOINTS } from '@/services/api/endpoints.generated'
import {
  dispatchFileOpenEvent,
  isPluginViewer,
  resolveFileViewer,
  type ResolvedFileViewer,
} from '@/services/fileViewerRegistry'
import {
  emitFileChange,
  getFileEditorData,
  registerFileEditor,
  updateFileEditorData,
} from '@/stores/fileEditorRegistry'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import { useWidgetEventStore } from '@/stores/widgetEventStore'
import { apiClient } from './api/client'

/** 编辑器类型 */
export type EditorType = 'ide' | 'builtin' | 'external'

/**
 * 打开文件的初始加载窗口（字节，用户裁定 2026-10-24 按需加载）：
 * 大文件首屏只取头部，剩余经「加载更多/加载全部」分页拉取（file-content
 * offset/limit 字节窗口），不做整文件载入。
 */
export const FILE_OPEN_CHUNK_BYTES = 64 * 1024

/**
 * 可编辑文件体积上限（1MB，与 CodeEditor 大文件只读阈值同源单值）：
 * 超过则只读预览禁编辑；装配层外部修改轮询也以它为跳过线（只读无编辑
 * 冲突面）。单源在此防 CodeEditor（懒加载 chunk）被装配层静态引用拖回主包。
 */
export const EDITABLE_FILE_MAX_BYTES = 1_000_000

/** file-content GET 响应体（打开面，含分页字段） */
interface FileContentResp {
  success: boolean
  content?: string
  message?: string
  size?: number
  /** 窗口末字节位（分页时由后端返回；缺省按全量处理） */
  loaded?: number
  /** 是否仍有未加载内容（分页时由后端返回） */
  has_more?: boolean
}

/**
 * 拉取文件内容窗口（任务工作空间优先，未命中回退 _local 项目根）。
 * 后端 success=false / HTTP 异常都抛 Error（message 如实透传给上层通知）。
 */
async function fetchFileWindow(
  filePath: string,
  containerTaskId: string | undefined,
  offset: number,
  limit: number | null,
): Promise<FileContentResp> {
  const resolvedContainerId = containerTaskId || '_local'
  const request = (cid: string) =>
    apiClient
      .get(
        WORKSPACE_SERVICE_ENDPOINTS.workspaces_file_content_get.replace(
          '{container_task_id}',
          cid,
        ),
        {
          params: limit !== null ? { path: filePath, offset, limit } : { path: filePath, offset },
        },
      )
      .then((resp) => resp.data as FileContentResp)

  let resp = await request(resolvedContainerId)
  // 任务工作空间未找到文件时，回退到项目根目录 _local 重试
  if (!resp?.success && resolvedContainerId !== '_local') {
    resp = await request('_local')
  }
  if (!resp?.success) {
    throw new Error(resp?.message || '读取文件失败')
  }
  return resp
}

/** 打开文件的统一入口 根据文件后缀从后端配置解析编辑器类型，然后路由到对应的打开方式： */
export async function openFile(
  filePath: string,
  options?: {
    containerTaskId?: string
  },
): Promise<{ success: boolean; editor: EditorType; message?: string }> {
  // 声明驱动路由（ADR 2026-10-24-file-viewer-declarative）：插件 file_viewers
  // 声明命中 → webview 沙箱页签（内容取流与渲染归插件页面，不走文本链）；
  // 未命中（含自带来源的内置查看器）→ 下方内置链路
  const resolved = resolveFileViewer(filePath)
  if (isPluginViewer(resolved)) {
    openPluginViewerTab(filePath, resolved, options?.containerTaskId)
    return { success: true, editor: 'builtin' }
  }

  const tabId = `file-local-${filePath.replace(/[/\\]/g, '_')}`
  const layoutStore = useLayoutModeStore.getState()

  // 如果 Tab 已存在，直接激活
  const existingTab = layoutStore.workspaceTabs.find(t => t.id === tabId)
  if (existingTab) {
    layoutStore.setActiveTab(tabId)
    return { success: true, editor: 'builtin' }
  }

  try {
    const resp = await fetchFileWindow(
      filePath,
      options?.containerTaskId,
      0,
      FILE_OPEN_CHUNK_BYTES,
    )
    const fileName = filePath.split(/[/\\]/).pop() || filePath
    registerFileEditor(tabId, {
      filePath,
      fileName,
      content: resp.content ?? '',
      size: resp.size,
      containerTaskId: options?.containerTaskId || '_local',
      // has_more=false（含全量小文件）→ undefined 即全量标记（可编辑）
      loadedBytes: resp.has_more === false ? undefined : resp.loaded,
    })
    layoutStore.addWorkspaceTab({
      id: tabId,
      title: fileName,
      icon: '📄',
      moduleId: '__file_editor__',
      isActive: true,
      isPinned: false,
    })
    return { success: true, editor: 'builtin' }
  } catch (error) {
    // 失败如实返回（message 供上层 reportError 通知中心弹出）——
    // 打开失败用户必须可见，禁止静默吞掉
    return {
      success: false,
      editor: 'builtin',
      message: error instanceof Error ? error.message : '打开文件失败',
    }
  }
}

/**
 * 打开插件查看器页签（webview 沙箱）：props 来自声明 render.props（透传
 * WebviewWidgetProps 形态），文件坐标经 widgetEventStore 'file.open' 下行
 * 事件注入页面（含延迟重推覆盖页面加载窗口）。同文件重复打开去重激活。
 */
export function openPluginViewerTab(
  filePath: string,
  resolved: ResolvedFileViewer,
  containerTaskId?: string,
): string {
  const viewer = resolved.viewer
  const tabId = `file-view-${viewer.id}-${filePath.replace(/[/\\]/g, '_')}`
  const widgetId =
    (viewer.render.props?.widgetId as string | undefined) ?? `file-viewer-${viewer.id}`
  const layoutStore = useLayoutModeStore.getState()

  const existingTab = layoutStore.workspaceTabs.find(t => t.id === tabId)
  if (existingTab) {
    layoutStore.setActiveTab(tabId)
  } else {
    layoutStore.addWorkspaceTab({
      id: tabId,
      title: filePath.split(/[/\\]/).pop() || filePath,
      icon: '📄',
      moduleId: '__file_viewer__',
      component: 'webview',
      props: { ...(viewer.render.props ?? {}), widgetId },
      isActive: true,
      isPinned: false,
    })
  }
  dispatchFileOpenEvent(
    (ev) => useWidgetEventStore.getState().dispatchWidgetEvent(ev),
    widgetId,
    filePath,
    containerTaskId,
  )
  return tabId
}

/**
 * 加载更多文件内容（编辑器「加载更多/加载全部」入口）：
 * 自已加载字节位继续拉取并追加到注册表，返回追加后是否仍有未加载内容。
 */
export async function loadMoreFileContent(
  tabId: string,
  opts?: { loadAll?: boolean },
): Promise<boolean> {
  const data = getFileEditorData(tabId)
  if (!data) return false
  const offset = data.loadedBytes ?? 0
  const resp = await fetchFileWindow(
    data.filePath,
    data.containerTaskId,
    offset,
    opts?.loadAll ? null : FILE_OPEN_CHUNK_BYTES,
  )
  const appended = resp.content ?? ''
  const newContent = data.content + appended
  updateFileEditorData(tabId, {
    content: newContent,
    size: resp.size,
    loadedBytes: resp.has_more === false ? undefined : resp.loaded,
  })
  emitFileChange(tabId, newContent, resp.size)
  return resp.has_more !== false
}
