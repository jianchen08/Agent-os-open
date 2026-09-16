/** UI Schema 解析器 解析后端模块的 UI Schema，输出类型安全的解析结果 */

import { WORKSPACE_SERVICE_ENDPOINTS } from '@/services/api/endpoints.generated'
import type {
  DataSourceRef,
  ResolvedDataSource,
} from '@/types/schema'

/** 解析数据源引用 格式：module://collection 或 module://collection?param=value */
export function parseDataSourceRef(ref: string): DataSourceRef {
  const match = ref.match(/^([\w-]+):\/\/([^?]+)(?:\?(.+))?$/)
  if (!match) {
    throw new Error(`无效的数据源引用格式: ${ref}`)
  }

  const [, moduleId, collection, queryString] = match
  const query: Record<string, string> = {}

  if (queryString) {
    const params = new URLSearchParams(queryString)
    params.forEach((value, key) => {
      query[key] = value
    })
  }

  return {
    moduleId,
    collection,
    query,
  }
}

/** 解析数据源引用为 API 端点 */
export function resolveDataSource(ref: DataSourceRef): ResolvedDataSource {
  let endpoint: string

  // workspace:// 协议特殊处理：workspace_service 插件文件树端点
  // （GET /ext/workspace_service/workspaces/{id}/file-tree，200 信封内可携带
  // workspace_status 业务态；404 = 归属闸拒绝，信封 error 有可读原因）
  if (ref.moduleId === 'workspace') {
    endpoint = WORKSPACE_SERVICE_ENDPOINTS.workspaces_file_tree.replace('{container_task_id}', ref.collection)
  } else {
    endpoint = `/api/v1/modules/${ref.moduleId}/data/${ref.collection}`
  }

  const params: Record<string, unknown> = { ...ref.query }

  if (ref.sort) params._sort = ref.sort
  if (ref.pagination) {
    params._page = ref.pagination.page
    params._pageSize = ref.pagination.pageSize
  }

  return {
    endpoint,
    method: 'GET',
    params,
    supportsPolling: true,
    pollInterval: ref.query?.pollInterval as number | undefined,
  }
}
