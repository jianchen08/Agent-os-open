/**
 * 文件编辑器 Tab 数据注册表
 *
 * 跨组件数据传递层：FiveSpaceLayout / useInteractionHandler 等写入 → CodeEditor/FilePreview 读取。
 * 使用 module-level Map 存储 + localStorage write-through 持久化，保证刷新页面后
 * 已打开的文件 Tab 仍能从注册表中恢复内容。
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
  /** 是否正在加载（运行时状态，不持久化） */
  loading?: boolean
  /** 附件直链 URL（如 /uploads/xxx.pdf）；优先于 containerTaskId 拼接的 workspaces URL */
  url?: string
}

/** 文件变更监听器回调类型 */
export type FileChangeListener = (newContent: string, newSize?: number) => void

/** localStorage 持久化 key */
const STORAGE_KEY = 'file-editor-registry'

/** 单文件最大持久化体积（256 KB），超出则跳过落盘 */
const MAX_PERSIST_SIZE = 256 * 1024

/**
 * 从 localStorage 还原 editorDataMap
 *
 * 防御性解析：JSON 损坏、字段缺失、quota 异常都静默回退到空 Map。
 */
function _loadFromStorage(): Map<string, FileEditorData> {
  if (typeof localStorage === 'undefined') return new Map()
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return new Map()
    const parsed = JSON.parse(raw) as Record<string, FileEditorData>
    const map = new Map<string, FileEditorData>()
    for (const [tabId, data] of Object.entries(parsed)) {
      if (data && typeof data.filePath === 'string') {
        map.set(tabId, data)
      }
    }
    return map
  } catch {
    return new Map()
  }
}

/**
 * 把 editorDataMap 序列化写入 localStorage
 *
 * - 剥离 loading 等运行时字段
 * - 超过 MAX_PERSIST_SIZE 的文件跳过持久化（避免单个大文件撑爆 localStorage）
 * - quota 异常静默忽略
 */
function _saveToStorage(): void {
  if (typeof localStorage === 'undefined') return
  try {
    const obj: Record<string, Omit<FileEditorData, 'loading'>> = {}
    for (const [tabId, data] of editorDataMap) {
      if ((data.content?.length ?? 0) > MAX_PERSIST_SIZE) continue
      const { loading: _l, ...rest } = data
      obj[tabId] = rest
    }
    localStorage.setItem(STORAGE_KEY, JSON.stringify(obj))
  } catch {
    // 配额溢出或权限受限，静默丢弃
  }
}

/** 内部存储：tabId → FileEditorData（启动时从 localStorage 还原） */
const editorDataMap: Map<string, FileEditorData> = _loadFromStorage()

/** 文件变更监听器存储：tabId → Set<listener>（运行时状态，不持久化） */
const fileChangeListeners = new Map<string, Set<FileChangeListener>>()

/**
 * 注册文件编辑器数据
 *
 * @param tabId - 工作区 Tab ID
 * @param data - 完整的文件编辑器数据
 */
export function registerFileEditor(tabId: string, data: FileEditorData): void {
  editorDataMap.set(tabId, data)
  _saveToStorage()
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
    _saveToStorage()
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
  fileChangeListeners.delete(tabId)
  _saveToStorage()
}

/**
 * 订阅文件内容变更事件
 *
 * 当文件被外部修改并重新加载时，触发监听器回调。
 *
 * @param tabId - 工作区 Tab ID
 * @param listener - 变更回调函数
 */
export function subscribeFileChange(tabId: string, listener: FileChangeListener): void {
  if (!fileChangeListeners.has(tabId)) {
    fileChangeListeners.set(tabId, new Set())
  }
  fileChangeListeners.get(tabId)!.add(listener)
}

/**
 * 取消订阅文件内容变更事件
 *
 * @param tabId - 工作区 Tab ID
 * @param listener - 变更回调函数
 */
export function unsubscribeFileChange(tabId: string, listener: FileChangeListener): void {
  fileChangeListeners.get(tabId)?.delete(listener)
}

/**
 * 触发文件内容变更事件
 *
 * 当检测到文件被外部修改时调用，通知所有订阅者。
 *
 * @param tabId - 工作区 Tab ID
 * @param newContent - 新的文件内容
 * @param newSize - 新的文件大小（可选）
 */
export function emitFileChange(tabId: string, newContent: string, newSize?: number): void {
  const listeners = fileChangeListeners.get(tabId)
  if (listeners) {
    for (const listener of listeners) {
      try {
        listener(newContent, newSize)
      } catch {
        // 监听器异常不影响其他监听器
      }
    }
  }
}
