/** UI Schema 解析器 解析后端模块的 UI Schema，输出类型安全的解析结果 */

import type {
  ModuleUISchema,
  ParsedSchema,
  DataSourceRef,
  ResolvedDataSource,
} from '@/types/schema'

/** 解析模块 UI Schema */
export function parseSchema(schema: ModuleUISchema): ParsedSchema {
  return {
    raw: schema,
    identity: schema.identity,
    actions: schema.actions,
    rendering: schema.rendering,
    clients: schema.clients,
    parsedAt: Date.now(),
    versionHash: computeSchemaHash(schema),
  }
}

/** 计算 Schema 版本哈希 */
function computeSchemaHash(schema: ModuleUISchema): string {
  const raw = JSON.stringify(schema)
  let hash = 0
  for (let i = 0; i < raw.length; i++) {
    const char = raw.charCodeAt(i)
    hash = (hash << 5) - hash + char
    hash |= 0
  }
  return hash.toString(36)
}

/** 解析数据源引用 格式：module://collection 或 module://collection?param=value */
export function parseDataSourceRef(ref: string): DataSourceRef {
  const match = ref.match(/^([\w-]+):\/\/([^\?]+)(?:\?(.+))?$/)
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

 // workspace:// 协议特殊处理
  // 该端点不存在，返回 404。
  if (ref.moduleId === 'workspace') {
    endpoint = `/api/v1/workspaces/${ref.collection}/file-tree`
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

/** 验证 Schema 格式 */
export function validateSchema(schema: unknown): schema is ModuleUISchema {
  if (!schema || typeof schema !== 'object') return false
  const s = schema as Record<string, unknown>

  if (!s.identity || typeof s.identity !== 'object') return false
  if (!Array.isArray(s.actions)) return false
  if (!s.rendering || typeof s.rendering !== 'object') return false
  if (!s.clients || typeof s.clients !== 'object') return false

  const identity = s.identity as Record<string, unknown>
  if (!identity.id || !identity.name || !identity.version) return false

  return true
}
