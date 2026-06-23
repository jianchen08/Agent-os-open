/**
 * 通用配置路由包装组件
 *
 * 路由使用 /settings/generic/* 通配符，支持多段 configPath（如 system/memory_storage）。
 * 从 URL 中读取通配符部分作为 configPath，查找注册表元数据，渲染 GenericConfigPage。
 * 若 configPath 不在注册表中则显示明确错误，而非静默跳转。
 */

import { Link, useParams } from 'react-router-dom'
import { GenericConfigPage } from '@/components/config/GenericConfigPage'
import { findConfigEntry } from '@/constants/genericConfigs'

/**
 * 通用配置路由包装组件
 */
export function GenericConfigRoute() {
  // react-router-dom v6: 通配符 * 路由的参数 key 为 '*'
  const configPath = useParams<'*'>()['*']

  if (!configPath) {
    return (
      <div className="flex h-screen flex-col items-center justify-center gap-4 p-8">
        <div className="text-6xl">❌</div>
        <h2 className="text-xl font-semibold">配置路径缺失</h2>
        <p className="text-muted-foreground text-center">
          URL 中未包含配置路径参数，请从设置中心重新进入。
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

  const entry = findConfigEntry(configPath)
  if (!entry) {
    return (
      <div className="flex h-screen flex-col items-center justify-center gap-4 p-8">
        <div className="text-6xl">⚠️</div>
        <h2 className="text-xl font-semibold">未知的配置项</h2>
        <p className="text-muted-foreground text-center">
          配置路径 <code className="bg-muted rounded px-1 text-sm">{configPath}</code> 未注册，可能已被移除或尚不支持。
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
      configPath={entry.configPath}
      title={entry.title}
      description={entry.description}
      labelMap={entry.labelMap}
    />
  )
}
