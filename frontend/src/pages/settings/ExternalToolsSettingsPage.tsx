/**
 * 外部工具配置页面
 *
 * 管理默认、Godot、VSCode 等外部工具配置。
 * 映射后端配置：external_tools/default, external_tools/godot, external_tools/vscode
 */

import { CategoryConfigPage } from '@/components/config/CategoryConfigPage'
import type { CategoryTabConfig } from '@/components/config/CategoryConfigPage'

const TABS: CategoryTabConfig[] = [
  { configPath: 'external_tools/default', title: '默认' },
  { configPath: 'external_tools/godot', title: 'Godot' },
  { configPath: 'external_tools/vscode', title: 'VSCode' },
]

/**
 * 外部工具配置页面组件
 */
export function ExternalToolsSettingsPage() {
  return (
    <CategoryConfigPage
      title="外部工具"
      description="管理外部工具连接与配置"
      tabs={TABS}
    />
  )
}
