/**
 * 安全配置页面
 *
 * 管理命令黑名单、路径保护、SSRF 防护规则和高风险操作审批策略。
 * 映射后端配置：isolation/security_rules, isolation/approval
 */

import { CategoryConfigPage } from '@/components/config/CategoryConfigPage'
import type { CategoryTabConfig } from '@/components/config/CategoryConfigPage'

const TABS: CategoryTabConfig[] = [
  { configPath: 'isolation/security_rules', title: '安全规则' },
  { configPath: 'isolation/approval', title: '审批配置' },
]

/**
 * 安全配置页面组件
 */
export function SecuritySettingsPage() {
  return (
    <CategoryConfigPage
      title="安全配置"
      description="命令黑名单、路径保护、审批策略"
      tabs={TABS}
    />
  )
}
