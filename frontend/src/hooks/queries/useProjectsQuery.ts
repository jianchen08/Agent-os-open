/**
 * 项目登记列表 query（服务端状态 query 化，OBS-R258-1 四态约定标准入口）
 *
 * 项目 = 文件夹 + 登记（ADR 2026-08-27）：登记行是 PipelineManagerWidget
 * 项目分组节点/列表登记行的数据源。唯一缓存 = TanStack Query
 * （queryKeys.projectsRegistry）；登记写操作（删除项目）后由调用方经
 * queryKeys.projectsPrefix 批量失效。
 */

import { useQuery } from '@tanstack/react-query'
import { fetchProjects } from '@/services/api/tasks'
import { queryKeys } from '@/services/query/queryKeys'

/** 登记行变化频率低：窗口内重挂零请求 */
const PROJECTS_STALE_TIME = 30_000

/** 项目登记列表 query（limit 对齐面板展示容量） */
export function useProjectsQuery() {
  return useQuery({
    queryKey: queryKeys.projectsRegistry,
    queryFn: () => fetchProjects({ limit: 100 }),
    staleTime: PROJECTS_STALE_TIME,
  })
}
