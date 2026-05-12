/**
 * 文件打开服务
 *
 * 提供统一的文件打开入口，根据文件后缀从后端配置解析编辑器类型，
 * 然后路由到对应的打开方式（外部IDE / 内置编辑器 / 系统默认应用）。
 *
 * 暴露接口：
 * - openFile(filePath, options) - 打开文件的统一入口
 * - EditorType - 编辑器类型枚举
 */

import { resolveEditor } from './api/editorConfig'
import { openFileInIDE } from './api/workspaces'

/** 编辑器类型 */
export type EditorType = 'ide' | 'builtin' | 'external'

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
 * @param filePath - 文件路径，如 '/project/src/index.ts'
 * @param options - 可选参数
 * @param options.line - 跳转到的行号
 * @param options.column - 跳转到的列号
 * @param options.onBuiltinOpen - 内置编辑器打开回调，当编辑器类型为 builtin 或降级时调用
 * @returns 打开结果，包含 success、editor 和可选的 message
 */
export async function openFile(
  filePath: string,
  options?: {
    line?: number
    column?: number
    onBuiltinOpen?: (filePath: string, line?: number, column?: number) => void
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
          options?.onBuiltinOpen?.(filePath, options?.line, options?.column)
          return { success: true, editor: 'builtin', message: 'IDE不可用，已切换到内置编辑器' }
        }
        return { success: true, editor: 'ide' }
      }
      case 'builtin': {
        options?.onBuiltinOpen?.(filePath, options?.line, options?.column)
        return { success: true, editor: 'builtin' }
      }
      case 'external': {
        // 外部应用通过后端API打开（后续可扩展）
        options?.onBuiltinOpen?.(filePath, options?.line, options?.column)
        return { success: true, editor: 'external', message: '暂不支持外部应用，已使用内置编辑器' }
      }
      default: {
        options?.onBuiltinOpen?.(filePath, options?.line, options?.column)
        return { success: true, editor: 'builtin' }
      }
    }
  } catch {
    // 解析失败，降级到内置编辑器
    options?.onBuiltinOpen?.(filePath, options?.line, options?.column)
    return { success: true, editor: 'builtin', message: '解析失败，已使用内置编辑器' }
  }
}
