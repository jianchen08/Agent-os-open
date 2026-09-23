/**
 * 单条管道配置 query（服务端状态 query 化，OBS-R258-1 四态约定标准入口）
 *
 * 内核实际执行的管道配置（config/pipelines/<name>.yaml 镜像）：P7 端点
 * 原子写 + If-Match 乐观锁（etag 随数据返回）。保存成功后由消费方把编辑
 * 副本连同新 ETag 回填缓存（setQueryData），重进页面不重拉。
 */

import { useQuery } from '@tanstack/react-query'
import { getPipelineConfig } from '@/services/api/pipelineConfig'
import { queryKeys } from '@/services/query/queryKeys'

/** 配置变化频率低（保存经写端点主动回填缓存）：窗口内重进页面不重拉 */
const PIPELINE_CONFIG_STALE_TIME = 60_000

/** 管道配置 query（按配置名分条缓存；主面，四态消费见 PipelineSettingsPage） */
export function usePipelineConfigQuery(name: string) {
  return useQuery({
    queryKey: queryKeys.pipelineConfig(name),
    queryFn: () => getPipelineConfig(name),
    staleTime: PIPELINE_CONFIG_STALE_TIME,
  })
}
