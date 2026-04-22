/**
 * 模块配置页面
 *
 * 在设置中自动渲染所有已注册模块的配置面板
 * 使用 ModuleConfigRenderer 根据 Schema 自动生成表单
 */

import React, { useState, useEffect } from 'react'
import { schemaRegistry } from '@/services/schema/registry'
import { ModuleConfigRenderer } from '@/components/schema/ModuleConfigRenderer'
import type { ModuleRegistration } from '@/types/schema'

/**
 * 模块配置页面组件
 */
export function ModulesSettingsPage() {
  const [modules, setModules] = useState<ModuleRegistration[]>([])
  const [activeModule, setActiveModule] = useState<string | null>(null)
  const [configValues, setConfigValues] = useState<Record<string, Record<string, unknown>>>({})

  useEffect(() => {
    const updateModules = () => setModules(schemaRegistry.getEnabled())
    updateModules()
    const unsubscribe = schemaRegistry.subscribe(updateModules)
    return unsubscribe
  }, [])

  const activeReg = modules.find(m => m.schema.identity.id === activeModule)

  return (
    <div className="h-full flex">
      {/* 模块列表 */}
      <div className="w-64 border-r border-border overflow-y-auto">
        <div className="p-4 text-sm font-medium text-foreground border-b border-border">
          已安装模块 ({modules.length})
        </div>
        {modules.map(mod => (
          <button
            key={mod.schema.identity.id}
            className={`w-full text-left px-4 py-3 text-sm border-b border-border/50 transition-colors ${
              activeModule === mod.schema.identity.id
                ? 'bg-accent text-accent-foreground'
                : 'text-foreground hover:bg-accent/50'
            }`}
            onClick={() => setActiveModule(mod.schema.identity.id)}
          >
            <div className="flex items-center gap-2">
              {mod.schema.identity.icon && <span>{mod.schema.identity.icon}</span>}
              <span className="font-medium">{mod.schema.identity.name}</span>
            </div>
            <div className="text-xs text-muted-foreground mt-0.5">
              v{mod.schema.identity.version} · {mod.schema.identity.category}
            </div>
          </button>
        ))}
      </div>

      {/* 配置面板 */}
      <div className="flex-1 overflow-y-auto p-6">
        {activeReg ? (
          <div>
            <div className="flex items-center gap-3 mb-6">
              {activeReg.schema.identity.icon && (
                <span className="text-2xl">{activeReg.schema.identity.icon}</span>
              )}
              <div>
                <h2 className="text-lg font-semibold text-foreground">
                  {activeReg.schema.identity.name}
                </h2>
                <p className="text-sm text-muted-foreground">
                  {activeReg.schema.identity.description ?? '无描述'}
                </p>
              </div>
            </div>
            <ModuleConfigRenderer
              schema={activeReg.schema}
              values={configValues[activeModule!] ?? {}}
              onChange={(key, value) => {
                setConfigValues(prev => ({
                  ...prev,
                  [activeModule!]: {
                    ...(prev[activeModule!] ?? {}),
                    [key]: value,
                  },
                }))
              }}
            />
          </div>
        ) : (
          <div className="flex items-center justify-center h-full text-muted-foreground">
            选择左侧模块查看配置
          </div>
        )}
      </div>
    </div>
  )
}
