/**
 * 懒加载树数据源协议集合（目录来源可插拔的登记口）。
 *
 * 目录来源不同 = 传不同 dataSource ref（组件本体不动，FileTreeWidget 按本集合
 * 判定是否走单层列举+path 参数懒展开管线）：
 * - workspace://<containerTaskId>：任务工作空间文件树（workspace file-tree 端点）
 * - fsbrowse://<roots>：多端目录树索引（fs-tree 端点，根层=本地盘枚举；设备分支
 *   =盘根下设备名挂载锚目录，下一层自然呈现）
 *
 * 新目录来源 = 本集合扩协议 + parser.resolveDataSource 加端点映射，两处登记外
 * 零改动。
 */
export const LAZY_TREE_SOURCE_SCHEMES = ['workspace', 'fsbrowse'] as const

/** 判定 dataSource ref 是否走懒加载树管线（单层列举协议） */
export function isLazyTreeSource(dataSource: string | undefined): boolean {
  if (!dataSource) return false
  return LAZY_TREE_SOURCE_SCHEMES.some((scheme) => dataSource.startsWith(`${scheme}://`))
}
