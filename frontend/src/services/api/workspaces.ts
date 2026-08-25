/**
 * 工作空间 API 调用
 *
 * 封装工作空间相关的 REST API 请求。
 */

import { apiClient } from './client'
import { WORKSPACE_SERVICE_ENDPOINTS as W } from './endpoints.generated'

const M = W.workspaces_get // 模板：替换 {container_task_id}（其余端点同前缀模板）

/** 获取工作空间详情 */
export async function getWorkspace(containerTaskId: string): Promise<any> {
  return apiClient.get(M.replace('{container_task_id}', containerTaskId))
}

/** 获取工作空间下所有制品 */
export async function getWorkspaceArtifacts(containerTaskId: string): Promise<any> {
  return apiClient.get(W.workspaces_artifacts.replace('{container_task_id}', containerTaskId))
}

/** 获取文件目录树 */
export async function getFileTree(containerTaskId: string): Promise<any> {
  return apiClient.get(W.workspaces_file_tree.replace('{container_task_id}', containerTaskId))
}

/** 创建文件或目录 */
export async function createEntry(
  containerTaskId: string,
  path: string,
  type: 'file' | 'directory',
): Promise<any> {
  return apiClient.post(W.workspaces_create_entry.replace('{container_task_id}', containerTaskId), { path, type })
}

/** 删除文件或目录 */
export async function deleteEntry(
  containerTaskId: string,
  path: string,
): Promise<any> {
  return apiClient.delete(W.workspaces_delete_entry.replace('{container_task_id}', containerTaskId), { data: { path } })
}

/** 重命名文件或目录 */
export async function renameEntry(
  containerTaskId: string,
  oldPath: string,
  newName: string,
): Promise<any> {
  return apiClient.post(W.workspaces_rename_entry.replace('{container_task_id}', containerTaskId), {
    old_path: oldPath,
    new_name: newName,
  })
}

/** 移动文件或目录 */
export async function moveEntry(
  containerTaskId: string,
  sourcePath: string,
  destinationDir: string,
): Promise<any> {
  return apiClient.post(W.workspaces_move_entry.replace('{container_task_id}', containerTaskId), {
    source_path: sourcePath,
    destination_dir: destinationDir,
  })
}

/** 工作空间文件内容响应 */
export interface WorkspaceFileContentResponse {
  success: boolean
  content?: string
  message?: string
}

/**
 * 获取工作空间文件内容
 *
 * 通过 apiClient 携带 Authorization 头读取指定路径的文本文件内容
 * （HtmlPreviewWidget 的降级读取通道，html 属性优先时不会调用）。
 */
export async function getWorkspaceFileContent(
  containerTaskId: string,
  filePath: string,
): Promise<WorkspaceFileContentResponse> {
  const response = await apiClient.get<WorkspaceFileContentResponse>(
    W.workspaces_file_content_get.replace('{container_task_id}', containerTaskId),
    { params: { path: filePath } },
  )
  return response.data
}

/**
 * 在外部 IDE 中打开文件
 *
 * 通过 IDE 连接器在外部 IDE（如 VS Code）中打开指定文件。
 * 支持跳转到指定行和列。
 *
 * @param filePath - 文件路径
 * @param line - 行号（可选）
 * @param column - 列号（可选）
 * @returns 打开结果，包含 success 字段
 */
export async function openFileInIDE(): Promise<{ data: { success: boolean; message?: string } }> {
  // TODO: 实现 IDE 连接器集成
  // 当前返回失败，会触发降级到内置编辑器
  return {
    data: {
      success: false,
      message: 'IDE 连接器尚未实现，请使用内置编辑器',
    },
  }
}
