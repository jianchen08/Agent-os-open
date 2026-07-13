/**
 * 隔离配置页面
 *
 * 管理工作空间、容器、权限策略和隔离策略配置。
 * 映射后端配置：isolation/isolation_config, isolation/isolation_policy
 */

import { CategoryConfigPage } from '@/components/config/CategoryConfigPage'
import type { CategoryTabConfig } from '@/components/config/CategoryConfigPage'

const TABS: CategoryTabConfig[] = [
  { configPath: 'isolation/isolation_config', title: '隔离配置' },
  { configPath: 'isolation/isolation_policy', title: '隔离策略' },
]

/**
 * 隔离配置页面组件
 */
export function IsolationSettingsPage() {
  return (
    <CategoryConfigPage
      title="隔离配置"
      description="工作空间、容器、权限策略配置"
      tabs={TABS}
    />
  )
}
