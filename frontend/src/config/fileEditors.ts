/**
 * 文件编辑器/预览器配置
 *
 * 根据文件扩展名映射到对应的编辑器组件。
 * 支持通过扩展 "editors" 注册新的编辑器类型，
 * 支持通过 "fileTypeMap" 配置扩展名与编辑器的映射关系。
 *
 * @module config/fileEditors
 */

/** 编辑器类型定义 */
export interface EditorDefinition {
  /** 编辑器唯一标识 */
  id: string
  /** 编辑器显示名称 */
  label: string
  /** 编辑器组件类型标识（用于 widgetRegistry 查找） */
  component: string
  /** 是否为内置编辑器 */
  builtin: boolean
}

/** 文件类型映射规则 */
export interface FileTypeMapping {
  /** 文件扩展名（含点号，如 ".txt"） */
  extension: string
  /** 对应的编辑器 ID */
  editorId: string
}

/** 内置编辑器注册表 */
const editors: Record<string, EditorDefinition> = {
  text_editor: {
    id: 'text_editor',
    label: '文本编辑器',
    component: 'file_review',
    builtin: true,
  },
  image_viewer: {
    id: 'image_viewer',
    label: '图片查看器',
    component: 'image_preview',
    builtin: true,
  },
  html_preview: {
    id: 'html_preview',
    label: 'HTML 预览',
    component: 'html_preview',
    builtin: true,
  },
}

/** 文件扩展名 → 编辑器映射（小写） */
const fileTypeMap: Record<string, string> = {
  '.txt': 'text_editor',
  '.md': 'text_editor',
  '.markdown': 'text_editor',
  '.py': 'text_editor',
  '.js': 'text_editor',
  '.jsx': 'text_editor',
  '.ts': 'text_editor',
  '.tsx': 'text_editor',
  '.json': 'text_editor',
  '.yaml': 'text_editor',
  '.yml': 'text_editor',
  '.toml': 'text_editor',
  '.xml': 'text_editor',
  '.html': 'html_preview',
  '.htm': 'html_preview',
  '.css': 'text_editor',
  '.scss': 'text_editor',
  '.less': 'text_editor',
  '.vue': 'text_editor',
  '.svelte': 'text_editor',
  '.rs': 'text_editor',
  '.go': 'text_editor',
  '.java': 'text_editor',
  '.kt': 'text_editor',
  '.c': 'text_editor',
  '.cpp': 'text_editor',
  '.h': 'text_editor',
  '.hpp': 'text_editor',
  '.cs': 'text_editor',
  '.rb': 'text_editor',
  '.php': 'text_editor',
  '.swift': 'text_editor',
  '.sh': 'text_editor',
  '.bash': 'text_editor',
  '.bat': 'text_editor',
  '.ps1': 'text_editor',
  '.sql': 'text_editor',
  '.r': 'text_editor',
  '.lua': 'text_editor',
  '.pl': 'text_editor',
  '.dart': 'text_editor',
  '.zig': 'text_editor',
  '.ini': 'text_editor',
  '.cfg': 'text_editor',
  '.conf': 'text_editor',
  '.env': 'text_editor',
  '.gitignore': 'text_editor',
  '.dockerignore': 'text_editor',
  '.editorconfig': 'text_editor',
  '.eslintrc': 'text_editor',
  '.prettierrc': 'text_editor',
  '.properties': 'text_editor',
  '.log': 'text_editor',
  '.csv': 'text_editor',
  '.tsv': 'text_editor',
  '.svg': 'text_editor',
  '.graphql': 'text_editor',
  '.gql': 'text_editor',
  '.proto': 'text_editor',
  '.dockerfile': 'text_editor',
  '.makefile': 'text_editor',
  '.cmake': 'text_editor',
  '.gradle': 'text_editor',
  '.lock': 'text_editor',
  '.map': 'text_editor',
  '.png': 'image_viewer',
  '.jpg': 'image_viewer',
  '.jpeg': 'image_viewer',
  '.gif': 'image_viewer',
  '.webp': 'image_viewer',
  '.ico': 'image_viewer',
  '.bmp': 'image_viewer',
}

/** 默认编辑器 ID */
const DEFAULT_EDITOR = 'text_editor'

/**
 * 根据文件名获取对应的编辑器定义
 *
 * @param fileName - 文件名（如 "main.py"、"README.md"）
 * @returns 匹配的编辑器定义，未匹配时返回默认文本编辑器
 */
export function getEditorForFile(fileName: string): EditorDefinition {
  const ext = extractExtension(fileName)
  const editorId = fileTypeMap[ext] ?? fileTypeMap[ext.toLowerCase()] ?? DEFAULT_EDITOR
  return editors[editorId] ?? editors[DEFAULT_EDITOR]
}

/**
 * 从文件名中提取扩展名（含点号）
 *
 * 同时处理普通扩展名（".py"）和无扩展名的特殊文件（"Makefile"、".gitignore"）。
 *
 * @param fileName - 文件名
 * @returns 小写扩展名（如 ".py"），无扩展名时返回整个文件名的小写
 */
function extractExtension(fileName: string): string {
  const lastSlash = Math.max(fileName.lastIndexOf('/'), fileName.lastIndexOf('\\'))
  const baseName = fileName.substring(lastSlash + 1)

  if (baseName.startsWith('.') && baseName.lastIndexOf('.') === 0) {
    return baseName.toLowerCase()
  }

  const dotIndex = baseName.lastIndexOf('.')
  if (dotIndex === -1) {
    return baseName.toLowerCase()
  }

  return baseName.substring(dotIndex).toLowerCase()
}

/**
 * 判断文件是否为文本类型
 *
 * @param fileName - 文件名
 * @returns 是否为文本类型文件
 */
export function isTextFile(fileName: string): boolean {
  return getEditorForFile(fileName).id === 'text_editor'
}

/**
 * 注册新的编辑器类型
 *
 * @param definition - 编辑器定义
 */
export function registerEditor(definition: EditorDefinition): void {
  editors[definition.id] = definition
}

/**
 * 注册文件类型映射
 *
 * @param extension - 文件扩展名（如 ".py"）
 * @param editorId - 编辑器 ID
 */
export function registerFileTypeMapping(extension: string, editorId: string): void {
  fileTypeMap[extension.toLowerCase()] = editorId
}
