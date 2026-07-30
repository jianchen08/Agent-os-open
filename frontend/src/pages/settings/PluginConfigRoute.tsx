/**
 * 插件配置路由包装组件
 *
 * 路由格式：/settings/plugin/{pluginId}/{fileId}
 * 使用 0.2 插件配置 API（非旧 generic config），兼容深链/书签。
 * 设置主入口已改为左列表右内联，本页保留给直达 URL。
 */

import { Link, useParams } from 'react-router-dom'
import { PluginConfigEditor } from '@/components/config/PluginConfigEditor'
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
          URL 中未包含插件 ID 或配置文件 ID，请从设置页重新进入。
        </p>
        <Link
          to="/settings"
          className="bg-primary text-primary-foreground mt-2 rounded-lg px-4 py-2 text-sm hover:opacity-90"
        >
          返回设置
        </Link>
      </div>
    )
  }

  const panel = contributionRegistry.getSettingsPanel(pluginId)
  const configFile = panel?.configFiles.find((f) => f.id === fileId)
  const title = configFile?.label || fileId
  const description = panel
    ? `${panel.pluginName} · ${configFile?.path || fileId}`
    : `${pluginId} / ${fileId}`

  return (
    <div className="bg-background text-foreground flex h-screen flex-col overflow-hidden">
      <header className="flex h-12 shrink-0 items-center border-b px-4">
        <Link to="/settings" className="text-muted-foreground hover:text-foreground text-sm">
          &larr; 设置
        </Link>
        <h1 className="ml-4 text-base font-semibold">{title}</h1>
      </header>
      <div className="min-h-0 flex-1 p-4 sm:p-6">
        <PluginConfigEditor
          pluginId={pluginId}
          fileId={fileId}
          title={title}
          description={description}
          embedded
        />
      </div>
    </div>
  )
}
