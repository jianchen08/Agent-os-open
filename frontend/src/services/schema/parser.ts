/**
 * UI Schema 解析器
 *
 * 解析后端模块的 UI Schema，输出类型安全的解析结果
 */

import type {
  ModuleUISchema,
  ParsedSchema,
  DataSourceRef,
  ResolvedDataSource,
} from '@/types/schema'

/**
 * 解析模块 UI Schema
 *
 * @param schema - 原始模块 Schema 对象
 * @returns 包含解析结果和时间戳的 ParsedSchema
 */
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

/**
 * 计算 Schema 版本哈希
 *
 * @param schema - 模块 Schema 对象
 * @returns 基于 Schema 内容计算的哈希字符串
 */
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

/**
 * 解析数据源引用
 * 格式：module://collection 或 module://collection?param=value
 *
 * @param ref - 数据源引用字符串
 * @returns 解析后的 DataSourceRef 对象
 * @throws 当引用格式无效时抛出错误
 */
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

/**
 * 解析数据源引用为 API 端点
 *
 * @param ref - 数据源引用对象
 * @returns 包含端点、方法和参数的 ResolvedDataSource
 */
export function resolveDataSource(ref: DataSourceRef): ResolvedDataSource {
  let endpoint: string

  // BUG-FIX-fix_20260512_001: workspace:// 协议特殊处理
  // 问题根因: resolveDataSource 将所有协议统一解析为 /api/modules/{moduleId}/data/{collection}，
  //           导致 workspace://{containerId} 被解析为 /api/modules/workspace/data/{containerId}，
  //           该端点不存在，返回 404。
  // 修复方案: 对 workspace:// 协议单独映射到 /api/v1/workspaces/{containerId}/file-tree
  // 影响范围: 工作区面板文件树加载
  // 修复日期: 2026-05-12
  if (ref.moduleId === 'workspace') {
    endpoint = `/api/v1/workspaces/${ref.collection}/file-tree`
  } else {
    endpoint = `/api/modules/${ref.moduleId}/data/${ref.collection}`
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

/**
 * 验证 Schema 格式
 *
 * @param schema - 待验证的 Schema 对象
 * @returns 类型谓词，判断是否为有效的 ModuleUISchema
 */
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
