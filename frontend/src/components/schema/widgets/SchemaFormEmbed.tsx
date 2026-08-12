/**
 * SchemaFormEmbed 组件
 *
 * 内嵌 Agent 配置编辑表单：
 * 1. 并行加载 字段 Schema（GET /api/v1/agents/schema）+ yaml 配置（GET /api/v1/agents/{id}/config）
 * 2. yaml 解析为表单初始值，SchemaDriver 渲染
 * 3. 提交时表单值序列化回 yaml，PUT /api/v1/agents/{id}/config 写回
 *
 * 同时服务两个入口：
 * - SettingsHubWidget 的 `schema:<agentId>` 内嵌分支
 * - AgentsPage 的 AgentConfigModal（Dialog 包裹）
 */

import { useCallback, useEffect, useState } from 'react'
import { getAgentConfig, getAgentSchema, putAgentConfig } from '@/services/api/agents'
import { SchemaDriver } from '@/services/schema/SchemaDriver'
import { parseYamlObject, serializeYaml } from '@/services/schema/yaml'
import type { UIInputFormField } from '@/types/schema'

/** SchemaFormEmbed 组件属性 */
export interface SchemaFormEmbedProps {
  /** Agent ID（配置文件名，对应 /api/v1/agents/{id}/config） */
  schemaId: string
  /** 保存成功回调（Modal 用它关闭并刷新） */
  onSaved?: () => void
}

/**
 * 内嵌 Agent 配置编辑表单
 *
 * @param props - schemaId（agent id）、onSaved 回调
 * @returns 加载中 / 错误 / 表单 三种状态
 */
export function SchemaFormEmbed({ schemaId, onSaved }: SchemaFormEmbedProps) {
  const [fields, setFields] = useState<UIInputFormField[]>([])
  const [initialValues, setInitialValues] = useState<Record<string, unknown>>({})
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [saveError, setSaveError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setLoadError(null)
    setSaved(false)

    Promise.all([getAgentSchema(), getAgentConfig(schemaId)])
      .then(([schema, config]) => {
        if (cancelled) return
        setFields(schema.fields ?? [])
        setInitialValues(parseYamlObject(config.yaml))
      })
      .catch((err: unknown) => {
        if (cancelled) return
        setLoadError(err instanceof Error ? err.message : '加载配置失败')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })

    return () => {
      cancelled = true
    }
  }, [schemaId])

  const handleSubmit = useCallback(
    async (values: Record<string, unknown>) => {
      setSaving(true)
      setSaved(false)
      setSaveError(null)
      try {
        const yaml = serializeYaml(values)
        await putAgentConfig(schemaId, yaml)
        setSaved(true)
        onSaved?.()
      } catch (err: unknown) {
        setSaveError(err instanceof Error ? err.message : '保存配置失败')
      } finally {
        setSaving(false)
      }
    },
    [schemaId, onSaved],
  )

  if (loading) {
    return <div className="text-muted-foreground p-4 text-sm">加载配置中...</div>
  }

  if (loadError) {
    return (
      <div className="border-destructive/30 bg-destructive/10 text-destructive rounded-md border p-4 text-sm">
        {error}
      </div>
    )
  }

  if (fields.length === 0) {
    return <div className="text-muted-foreground p-4 text-sm">暂无表单字段</div>
  }

  return (
    <div className="space-y-3">
      {saved && (
        <div className="border-status-success/30 bg-status-success/10 text-status-success rounded-md border px-3 py-2 text-sm">
          配置已保存
        </div>
      )}
      {saveError && (
        <div className="border-destructive/30 bg-destructive/10 text-destructive rounded-md border px-3 py-2 text-sm">
          {saveError}
        </div>
      )}
      <SchemaDriver
        fields={fields}
        initialValues={initialValues}
        onSubmit={handleSubmit}
        submitLabel={saving ? '保存中...' : '保存配置'}
        layout="double"
      />
    </div>
  )
}

export default SchemaFormEmbed
