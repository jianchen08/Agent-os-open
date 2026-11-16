/** @feature FP-0.2.多设备目录树 树数据源协议集合 | @ci: frontend-test */
// @feature: 多端目录树 fsbrowse 数据源 | @ci: frontend-test
/**
 * 懒加载树数据源协议集合（treeDataSources）与 fsbrowse 端点映射（parser）：
 * - isLazyTreeSource：workspace:// 与 fsbrowse:// 两协议可懒加载，其余/缺省不可
 * - resolveDataSource：fsbrowse:// → workspaces_fs_tree 端点（path 参数懒展开；
 *   query 透传 = 目录来源可插拔的根路径注入口）
 */
import { describe, expect, it } from 'vitest'
import { parseDataSourceRef, resolveDataSource } from '../parser'
import { isLazyTreeSource, LAZY_TREE_SOURCE_SCHEMES } from '../treeDataSources'

describe('treeDataSources — 懒加载树数据源协议集合', () => {
  it('workspace:// 与 fsbrowse:// 判定为可懒加载', () => {
    expect(isLazyTreeSource('workspace://task-1')).toBe(true)
    expect(isLazyTreeSource('fsbrowse://roots')).toBe(true)
  })

  it('其他协议与缺省不可懒加载（含前缀相似协议不误判）', () => {
    expect(isLazyTreeSource('tasks://messages')).toBe(false)
    expect(isLazyTreeSource(undefined)).toBe(false)
    expect(isLazyTreeSource('')).toBe(false)
    expect(isLazyTreeSource('fsbrowseX://roots')).toBe(false)
  })

  it('协议集合即两个已登记目录来源', () => {
    expect([...LAZY_TREE_SOURCE_SCHEMES]).toEqual(['workspace', 'fsbrowse'])
  })
})

describe('resolveDataSource — fsbrowse 多端目录树索引', () => {
  it('fsbrowse:// 解析为 fs-tree 端点（collection 不进路径）', () => {
    const resolved = resolveDataSource(parseDataSourceRef('fsbrowse://roots'))
    expect(resolved.endpoint).toBe('/ext/workspace_service/workspaces/fs-tree')
    expect(resolved.method).toBe('GET')
    expect(resolved.params).toEqual({})
  })

  it('query 参数透传（目录来源可插拔的根路径注入口）', () => {
    const resolved = resolveDataSource(parseDataSourceRef('fsbrowse://roots?base=D%3A%5C'))
    expect(resolved.endpoint).toBe('/ext/workspace_service/workspaces/fs-tree')
    expect(resolved.params).toEqual({ base: 'D:\\' })
  })
})
