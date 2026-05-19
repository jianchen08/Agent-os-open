/**
 * 文件编辑器 Tab 数据注册表
 *
 * 跨组件数据传递层：FiveSpaceLayout 写入 → CodeEditor/FilePreview 读取。
 * 使用 module-level Map 存储，不依赖 Zustand，轻量无副作用。
 *
 * @module stores/fileEditorRegistry
 */

/** 编辑器 Tab 数据 */
export interface FileEditorData {
  /** 文件路径（如 src/main.py） */
  filePath: string
  /** 文件名（如 main.py） */
  fileName: string
  /** 文件内容 */
  content: string
  /** 文件大小（字节） */
  size?: number
  /** 容器任务 ID（用于 API 调用） */
  containerTaskId: string
  /** 是否正在加载 */
  loading?: boolean
}

/** 内部存储：tabId → FileEditorData */
const editorDataMap = new Map<string, FileEditorData>()

/**
 * 注册文件编辑器数据
 *
 * @param tabId - 工作区 Tab ID
 * @param data - 完整的文件编辑器数据
 */
export function registerFileEditor(tabId: string, data: FileEditorData): void {
  editorDataMap.set(tabId, data)
}

/**
 * 获取文件编辑器数据
 *
 * @param tabId - 工作区 Tab ID
 * @returns 文件编辑器数据，不存在则返回 undefined
 */
export function getFileEditorData(tabId: string): FileEditorData | undefined {
  return editorDataMap.get(tabId)
}

/**
 * 更新文件编辑器数据（部分更新）
 *
 * @param tabId - 工作区 Tab ID
 * @param partial - 需要更新的字段
 */
export function updateFileEditorData(
  tabId: string,
  partial: Partial<FileEditorData>,
): void {
  const existing = editorDataMap.get(tabId)
  if (existing) {
    editorDataMap.set(tabId, { ...existing, ...partial })
  }
}

/**
 * 移除文件编辑器数据
 *
 * 在关闭 Tab 时调用，防止内存泄漏。
 *
 * @param tabId - 工作区 Tab ID
 */
export function removeFileEditorData(tabId: string): void {
  editorDataMap.delete(tabId)
}
