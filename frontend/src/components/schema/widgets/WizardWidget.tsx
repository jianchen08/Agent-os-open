/**
 * WizardWidget — 多步表单（widget 化 G5：富交互形态）
 *
 * 声明模型（ui_schema.widgets props）：
 *   { "type": "wizard",
 *     "props": {
 *       "steps": [
 *         { "title": "基本信息", "fields": [ ...UIInputFormField ] },
 *         { "title": "执行环境", "fields": [ ... ] }
 *       ],
 *       "submitLabel": "创建",
 *       "endpoint": "/ext/task_service/tasks/root",   // 末步提交（POST {pipeline_id,...values}）
 *       "eventName": "task.created",     // 提交成功发事件（G3 联动）
 *       "successText": "任务已创建"
 *     } }
 *
 * 行为：分步渲染（每步独立 RjsfForm，required 校验通过才前进）；跨步值在
 * 内部累积；末步提交全量值到 endpoint。
 */
import { useState } from 'react'
import { Button } from '@/components/ui/button'
import { toast } from '@/components/ui/sonner'
import { RjsfForm } from '@/services/schema/RjsfForm'
import { emitFormEvent } from '@/services/schema/formEventBus'
import { useAgentTabStore } from '@/stores/agentTabStore'
import type { UIInputFormField } from '@/types/schema'
import { usePipelineMessageStore } from '@/stores/pipelineMessageStore'
import { useSessionStore } from '@/stores/sessionStore'

function extractSteps(steps: unknown): Array<{ title: string; fields: UIInputFormField[] }> {
  if (!Array.isArray(steps)) return []
  return steps
    .filter((s): s is Record<string, unknown> => !!s && typeof s === 'object')
    .map((s) => ({
      title: typeof s.title === 'string' ? s.title : `步骤 ${Object.keys(s).length}`,
      fields: (Array.isArray(s.fields) ? s.fields : []).filter(
        (f): f is UIInputFormField =>
          !!f && typeof f === 'object' && typeof (f as UIInputFormField).name === 'string',
      ),
    }))
    .filter((s) => s.fields.length > 0)
}

export function WizardWidget(props: Record<string, unknown>) {
  const steps = extractSteps(props.steps)
  const endpoint = props.endpoint as string | undefined
  const eventName = props.eventName as string | undefined
  const successText = props.successText as string | undefined
  const submitLabel = (props.submitLabel as string) ?? '提交'
  const onSaved = props.onSaved as (() => void) | undefined

  const [stepIdx, setStepIdx] = useState(0)
  const [allValues, setAllValues] = useState<Record<string, unknown>>({})
  const [submitting, setSubmitting] = useState(false)

  const activeTabId = useAgentTabStore((s) => s.activeTabId)
  const tabs = useAgentTabStore((s) => s.tabs)
  const activePipelineId = usePipelineMessageStore((s) => s.activePipelineId)
  const sessionId = useSessionStore((s) => s.activeSessionId)
  const pipelineId = tabs.find((t) => t.id === activeTabId)?.pipelineRunId ?? activePipelineId ?? sessionId ?? ''

  if (steps.length === 0) {
    return (
      <div className="rounded-lg border border-dashed p-6 text-center">
        <p className="text-muted-foreground text-sm">暂无向导步骤（steps 为空）</p>
      </div>
    )
  }

  const step = steps[stepIdx]
  const isLast = stepIdx === steps.length - 1
  // 初值为本步字段在当前累积值中的子集
  const initialValues: Record<string, unknown> = {}
  for (const f of step.fields) {
    if (typeof f === 'object' && f !== null && typeof (f as { name?: unknown }).name === 'string') {
      const name = (f as { name: string }).name
      if (allValues[name] !== undefined) initialValues[name] = allValues[name]
    }
  }

  const handleStepSubmit = async (values: Record<string, unknown>) => {
    const merged = { ...allValues, ...values }
    if (!isLast) {
      setAllValues(merged)
      setStepIdx((i) => i + 1)
      return
    }
    // 末步提交全量累积值
    setSubmitting(true)
    try {
      if (endpoint) {
        const resp = await fetch(endpoint, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ pipeline_id: pipelineId, ...merged }),
        })
        const data = (await resp.json()) as { error?: string; reason?: string }
        if (data.error || data.reason) throw new Error(data.reason ?? data.error)
      }
      toast.success(successText ?? '提交成功')
      if (eventName) emitFormEvent(eventName, merged)
      onSaved?.()
    } catch (err) {
      toast.error('提交失败', {
        description: err instanceof Error ? err.message : String(err),
      })
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="space-y-3">
      {/* 步骤指示 */}
      <div className="flex items-center gap-1.5">
        {steps.map((s, i) => (
          <div
            key={s.title}
            data-testid={`wizard-step-${i}`}
            data-active={i === stepIdx}
            className={`flex-1 rounded px-2 py-1 text-center text-[11px] ${
              i === stepIdx
                ? 'bg-primary/15 text-primary'
                : i < stepIdx
                  ? 'text-muted-foreground'
                  : 'text-muted-foreground/50'
            }`}
          >
            {i + 1}. {s.title}
          </div>
        ))}
      </div>

      {/* 当前步表单 */}
      <RjsfForm
        key={`step-${stepIdx}`}
        fields={step.fields}
        initialValues={initialValues}
        submitLabel={isLast ? submitLabel : '下一步'}
        onSubmit={handleStepSubmit}
      />

      {/* 导航：上一步 */}
      {stepIdx > 0 && (
        <div className="flex justify-end">
          <Button
            variant="outline"
            size="sm"
            disabled={submitting}
            onClick={() => setStepIdx((i) => i - 1)}
          >
            上一步
          </Button>
        </div>
      )}
    </div>
  )
}
