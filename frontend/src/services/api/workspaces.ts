/**
 * 工作空间 API 调用
 *
 * 封装工作空间相关的 REST API 请求。
 */

import { apiClient } from './client'

const BASE = '/api/v1/workspaces'

/** 获取工作空间详情 */
export async function getWorkspace(containerTaskId: string): Promise<any> {
  return apiClient.get(`${BASE}/${containerTaskId}`)
}

/** 获取工作空间下所有制品 */
export async function getWorkspaceArtifacts(containerTaskId: string): Promise<any> {
  return apiClient.get(`${BASE}/${containerTaskId}/artifacts`)
}

/** 获取文件目录树 */
export async function getFileTree(containerTaskId: string): Promise<any> {
  return apiClient.get(`${BASE}/${containerTaskId}/file-tree`)
}

/** 创建文件或目录 */
export async function createEntry(
  containerTaskId: string,
  path: string,
  type: 'file' | 'directory',
): Promise<any> {
  return apiClient.post(`${BASE}/${containerTaskId}/create-entry`, { path, type })
}

/** 删除文件或目录 */
export async function deleteEntry(
  containerTaskId: string,
  path: string,
): Promise<any> {
  return apiClient.delete(`${BASE}/${containerTaskId}/entries`, { data: { path } })
}

/** 重命名文件或目录 */
export async function renameEntry(
  containerTaskId: string,
  oldPath: string,
  newName: string,
): Promise<any> {
  return apiClient.post(`${BASE}/${containerTaskId}/rename-entry`, {
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
  return apiClient.post(`${BASE}/${containerTaskId}/move-entry`, {
    source_path: sourcePath,
    destination_dir: destinationDir,
  })
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
export async function openFileInIDE(
  filePath: string,
  line?: number,
  column?: number,
): Promise<{ data: { success: boolean; message?: string } }> {
  // TODO: 实现 IDE 连接器集成
  // 当前返回失败，会触发降级到内置编辑器
  return {
    data: {
      success: false,
      message: 'IDE 连接器尚未实现，请使用内置编辑器',
    },
  }
}
