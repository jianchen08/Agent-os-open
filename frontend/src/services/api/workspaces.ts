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
