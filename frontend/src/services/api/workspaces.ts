/**
 * 工作空间 API 调用
 *
 * 封装工作空间相关的 REST API 请求。
 */

import { apiClient } from './client'
import { WORKSPACE_SERVICE_ENDPOINTS as W } from './endpoints.generated'

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

/* ── 读接口（扫描批K S8：store 手拼 URL 收口到服务层，端点/鉴权单一出口） ── */

/** 工作空间原始载荷（后端 camelCase 返回；字段可缺省由 store 归一化兜底） */
export type WorkspacePayload = Record<string, unknown>

/** 工作空间详情（workspaces_get） */
export async function getWorkspace(containerTaskId: string): Promise<WorkspacePayload> {
  const response = await apiClient.get<WorkspacePayload>(
    W.workspaces_get.replace('{container_task_id}', containerTaskId),
  )
  return response.data
}

/** 文件树节点原始载荷 */
export interface FileTreeNodePayload {
  name?: string
  type?: string
  path?: string
  artifactId?: string
  children?: FileTreeNodePayload[]
  metadata?: Record<string, unknown>
}

/** 工作空间目录树（workspaces_file_tree） */
export async function getWorkspaceFileTree(
  containerTaskId: string,
): Promise<{ tree: FileTreeNodePayload[] }> {
  const response = await apiClient.get<{ tree?: FileTreeNodePayload[] }>(
    W.workspaces_file_tree.replace('{container_task_id}', containerTaskId),
  )
  return { tree: response.data?.tree ?? [] }
}

/** 制品条目原始载荷 */
export type ArtifactPayload = Record<string, unknown>

/** 工作空间全部制品（workspaces_artifacts；无制品时 items 为空数组） */
export async function getWorkspaceArtifacts(
  containerTaskId: string,
): Promise<{ items: ArtifactPayload[] }> {
  const response = await apiClient.get<{ items?: ArtifactPayload[] }>(
    W.workspaces_artifacts.replace('{container_task_id}', containerTaskId),
  )
  return { items: response.data?.items ?? [] }
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
