/**
 * 插件配置路由包装组件
 *
 * 路由格式：/settings/plugin/{pluginId}/{fileId}
 * 从 ContributionRegistry 查找插件配置映射，渲染 GenericConfigPage。
 * 替代旧的专用设置页（ApiSettingsPage 等）。
 */

import { Link, useParams } from 'react-router-dom'
import { GenericConfigPage } from '@/components/config/GenericConfigPage'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'

/**
 * 插件配置路由包装组件
 */
export function PluginConfigRoute() {
  const { pluginId, fileId } = useParams<{ pluginId: string; fileId: string }>()

  if (!pluginId || !fileId) {
    return (
      <div className="flex h-screen flex-col items-center justify-center gap-4 p-8">
        <div className="text-6xl">❌</div>
        <h2 className="text-xl font-semibold">配置路径缺失</h2>
        <p className="text-muted-foreground text-center">
          URL 中未包含插件 ID 或配置文件 ID，请从设置中心重新进入。
        </p>
        <Link
          to="/settings"
          className="bg-primary text-primary-foreground mt-2 rounded-lg px-4 py-2 text-sm hover:opacity-90"
        >
          返回设置中心
        </Link>
      </div>
    )
  }

  const panel = contributionRegistry.getSettingsPanel(pluginId)
  if (!panel) {
    return (
      <div className="flex h-screen flex-col items-center justify-center gap-4 p-8">
        <div className="text-6xl">⚠️</div>
        <h2 className="text-xl font-semibold">插件未找到</h2>
        <p className="text-muted-foreground text-center">
          插件 <code className="bg-muted rounded px-1 text-sm">{pluginId}</code> 未注册配置面板，可能已被禁用或不存在。
        </p>
        <Link
          to="/settings"
          className="bg-primary text-primary-foreground mt-2 rounded-lg px-4 py-2 text-sm hover:opacity-90"
        >
          返回设置中心
        </Link>
      </div>
    )
  }

  const configFile = panel.configFiles.find((f) => f.id === fileId)
  if (!configFile) {
    return (
      <div className="flex h-screen flex-col items-center justify-center gap-4 p-8">
        <div className="text-6xl">⚠️</div>
        <h2 className="text-xl font-semibold">配置项未找到</h2>
        <p className="text-muted-foreground text-center">
          插件 <code className="bg-muted rounded px-1 text-sm">{panel.pluginName}</code> 中不存在配置项{' '}
          <code className="bg-muted rounded px-1 text-sm">{fileId}</code>。
        </p>
        <Link
          to="/settings"
          className="bg-primary text-primary-foreground mt-2 rounded-lg px-4 py-2 text-sm hover:opacity-90"
        >
          返回设置中心
        </Link>
      </div>
    )
  }

  return (
    <GenericConfigPage
      configPath={configFile.path}
      title={configFile.label}
      description={`${panel.pluginName} · ${configFile.label}`}
    />
  )
}
