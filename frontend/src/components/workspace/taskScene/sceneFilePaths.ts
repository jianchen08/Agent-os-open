/**
 * 产物路径工具（纯函数，从 TaskSceneArtifacts 拆出）
 *
 * 拆分理由：组件文件只导出组件（react-refresh/only-export-components）。
 * 这两个函数是「未命名」修复的行为锚（文件名取末段、目录取前缀），单测直接断言。
 */

/** 路径 → 文件名（取末段；末段为空回落整路径，不落「未命名」） */
export function baseNameOf(path: string): string {
  const normalized = path.replace(/\\/g, '/')
  const name = normalized.slice(normalized.lastIndexOf('/') + 1)
  return name || normalized
}

/** 路径 → 所在目录（无目录返回空串，调用方不渲染该列） */
export function dirNameOf(path: string): string {
  const normalized = path.replace(/\\/g, '/')
  const index = normalized.lastIndexOf('/')
  return index <= 0 ? '' : normalized.slice(0, index)
}
