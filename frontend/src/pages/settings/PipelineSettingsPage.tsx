/**
 * 管道配置页面
 *
 * 管理默认管道、L1/L2 Agent 管道插件链配置。
 * 映射后端配置：pipelines/default, pipelines/l1-main, pipelines/l2-evaluator, pipelines/l2-subtask
 */

import { CategoryConfigPage } from '@/components/config/CategoryConfigPage'
import type { CategoryTabConfig } from '@/components/config/CategoryConfigPage'

const TABS: CategoryTabConfig[] = [
  { configPath: 'pipelines/default', title: '默认' },
  { configPath: 'pipelines/l1-main', title: 'L1 主 Agent' },
  { configPath: 'pipelines/l2-evaluator', title: 'L2 评估' },
  { configPath: 'pipelines/l2-subtask', title: 'L2 子任务' },
]

/**
 * 管道配置页面组件
 */
export function PipelineSettingsPage() {
  return (
    <CategoryConfigPage
      title="管道配置"
      description="管道插件链与 Agent 管道配置"
      tabs={TABS}
    />
  )
}
