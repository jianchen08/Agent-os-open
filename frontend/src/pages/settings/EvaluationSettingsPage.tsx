/**
 * 评估配置页面
 *
 * 管理内置评估指标定义（文件检查、语义检查等）。
 * 映射后端配置：evaluation/evaluation_metrics
 */

import { CategoryConfigPage } from '@/components/config/CategoryConfigPage'
import type { CategoryTabConfig } from '@/components/config/CategoryConfigPage'

const TABS: CategoryTabConfig[] = [
  { configPath: 'evaluation/evaluation_metrics', title: '评估指标' },
]

/**
 * 评估配置页面组件
 */
export function EvaluationSettingsPage() {
  return (
    <CategoryConfigPage
      title="评估配置"
      description="内置评估指标定义与评估参数"
      tabs={TABS}
    />
  )
}
