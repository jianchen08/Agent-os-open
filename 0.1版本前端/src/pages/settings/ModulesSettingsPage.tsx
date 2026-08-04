/**
 * 模块配置页面
 *
 * 在设置中自动渲染所有已注册模块的配置面板
 * 使用 ModuleConfigRenderer 根据 Schema 自动生成表单
 */

import React, { useState, useEffect } from 'react'
import { ModuleConfigRenderer } from '@/components/schema/ModuleConfigRenderer'
import { schemaRegistry } from '@/services/schema/registry'
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

  const activeReg = modules.find((m) => m.schema.identity.id === activeModule)

  return (
    <div className="bg-background text-foreground flex h-screen flex-col overflow-hidden">
      <header className="flex h-12 shrink-0 items-center border-b px-4">
        <a href="/settings" className="text-muted-foreground hover:text-foreground text-sm">
          &larr; 返回设置
        </a>
        <h1 className="ml-4 text-base font-semibold">模块设置</h1>
      </header>
      <div className="flex flex-1 overflow-hidden">
      {/* 模块列表 */}
      <div className="border-border w-64 overflow-y-auto border-r">
        <div className="text-foreground border-border border-b p-4 text-sm font-medium">
          已安装模块 ({modules.length})
        </div>
        {modules.map((mod) => (
          <button
            key={mod.schema.identity.id}
            className={`border-border/50 w-full border-b px-4 py-3 text-left text-sm transition-colors ${
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
            <div className="text-muted-foreground mt-0.5 text-xs">
              v{mod.schema.identity.version} · {mod.schema.identity.category}
            </div>
          </button>
        ))}
      </div>

      {/* 配置面板 */}
      <div className="flex-1 overflow-y-auto p-3 sm:p-6">
        {activeReg ? (
          <div>
            <div className="mb-6 flex items-center gap-3">
              {activeReg.schema.identity.icon && (
                <span className="text-2xl">{activeReg.schema.identity.icon}</span>
              )}
              <div>
                <h2 className="text-foreground text-lg font-semibold">
                  {activeReg.schema.identity.name}
                </h2>
                <p className="text-muted-foreground text-sm">
                  {activeReg.schema.identity.description ?? '无描述'}
                </p>
              </div>
            </div>
            <ModuleConfigRenderer
              schema={activeReg.schema}
              values={configValues[activeModule!] ?? {}}
              onChange={(key, value) => {
                setConfigValues((prev) => ({
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
          <div className="text-muted-foreground flex h-full items-center justify-center">
            选择左侧模块查看配置
          </div>
        )}
      </div>
      </div>
    </div>
  )
}
