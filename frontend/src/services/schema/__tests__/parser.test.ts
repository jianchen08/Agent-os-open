// @feature: FP-0.2.四 前端Schema | @ci: frontend-test
/**
 * schema/parser 单测：数据源引用解析（parseDataSourceRef / resolveDataSource）
 */
import { describe, it, expect } from 'vitest'
import { parseDataSourceRef, resolveDataSource } from '../parser'

describe('parseDataSourceRef', () => {
  it('解析 module://collection 基础格式', () => {
    const ref = parseDataSourceRef('tasks://messages')
    expect(ref).toEqual({ moduleId: 'tasks', collection: 'messages', query: {} })
  })

  it('解析带查询参数的引用（多参数、URL 编码值）', () => {
    const ref = parseDataSourceRef('tasks://messages?status=open&limit=10&q=a%20b')
    expect(ref.moduleId).toBe('tasks')
    expect(ref.collection).toBe('messages')
    expect(ref.query).toEqual({ status: 'open', limit: '10', q: 'a b' })
  })

  it('解析无查询参数时 query 为空对象', () => {
    const ref = parseDataSourceRef('memory://notes')
    expect(ref.query).toEqual({})
  })

  it('非法格式（缺协议/缺集合）抛错', () => {
    expect(() => parseDataSourceRef('not-a-ref')).toThrow(/无效的数据源引用格式/)
    expect(() => parseDataSourceRef('module://')).toThrow(/无效的数据源引用格式/)
  })
})

describe('resolveDataSource', () => {
  it('普通模块解析为 /api/v1/modules/... 端点，GET + 支持轮询', () => {
    const resolved = resolveDataSource({ moduleId: 'tasks', collection: 'messages', query: {} })
    expect(resolved.endpoint).toBe('/api/v1/modules/tasks/data/messages')
    expect(resolved.method).toBe('GET')
    expect(resolved.supportsPolling).toBe(true)
    expect(resolved.params).toEqual({})
  })

  it('workspace 模块走 file-tree 端点并替换 container_task_id', () => {
    const resolved = resolveDataSource({ moduleId: 'workspace', collection: 'task-42', query: {} })
    expect(resolved.endpoint).toBe(
      '/ext/workspace_service/workspaces/task-42/file-tree',
    )
  })

  it('sort 与 pagination 映射为 _sort/_page/_pageSize 参数', () => {
    const resolved = resolveDataSource({
      moduleId: 'tasks',
      collection: 'messages',
      query: {},
      sort: 'created_at',
      pagination: { page: 2, pageSize: 20 },
    })
    expect(resolved.params).toEqual({ _sort: 'created_at', _page: 2, _pageSize: 20 })
  })

  it('无 sort/pagination 时不注入对应参数', () => {
    const resolved = resolveDataSource({ moduleId: 'tasks', collection: 'messages', query: {} })
    expect(resolved.params).not.toHaveProperty('_sort')
    expect(resolved.params).not.toHaveProperty('_page')
  })

  it('query 透传并提取 pollInterval（pollInterval 同时保留在 params 中）', () => {
    const resolved = resolveDataSource({
      moduleId: 'tasks',
      collection: 'messages',
      query: { pollInterval: 5000, status: 'open' },
    })
    // 现状契约：params 是 query 的浅拷贝，pollInterval 一并透传
    expect(resolved.params).toEqual({ status: 'open', pollInterval: 5000 })
    expect(resolved.pollInterval).toBe(5000)
  })

  it('无 pollInterval 时 pollInterval 为 undefined', () => {
    const resolved = resolveDataSource({ moduleId: 'tasks', collection: 'messages', query: {} })
    expect(resolved.pollInterval).toBeUndefined()
  })
})
