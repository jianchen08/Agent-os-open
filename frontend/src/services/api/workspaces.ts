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
