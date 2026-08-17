/**
 * 表单交互组件（渲染/校验引擎已统一到 RjsfForm）
 *
 * 聊天/工作区空间的动态表单 widget。字段词汇表（input/textarea/select/toggle/
 * number/slider/color/date/multiselect/radio/checkbox）已并入 UIInputFormField
 * 统一类型，本组件只做 props 收窄与提交回调透传。
 *
 * 渲染形态：
 * 1. 标准表单：多字段/复杂表单走 RjsfForm，props.onSubmit 或 props.endpoint 提交。
 * 2. 紧凑下拉（compact）：单 select 字段 + endpoint/onChange 时自动启用——图标按钮 +
 *    DropdownMenu，点选即提交/回调，适合插件声明式选择器（如权限模式切换、
 *    思考强度跟随管道标签）。高风险操作可在端点内经 human-interaction 弹
 *    审批窗确认（fetch 挂起等待）。
 *
 * 提交/受控模式（endpoint 与受控互斥）：
 * - props.endpoint（声明 JSON 可传字符串）：POST {pipeline_id: 当前选中管道, ...values}
 *   到该端点，展示提交中/成功/失败状态。pipeline_id 跟随当前选中的管道标签
 *   （agentTabStore.activeTabId → pipelineRunId，回退 store 级 activePipelineId /
 *   sessionId）——权限模式按管道隔离，切换标签即切换目标。
 * - props.onSubmit：显式 JS 回调（宿主注入场景）。
 * - props.value + props.onChange：受控模式（宿主注入当前值 + 变更回调，
 *   值跟随宿主 state，如思考强度随管道标签变化）。
 *
 * @module FormWidget
 */

import { useState } from 'react'
import { Check, ChevronDown } from '@/assets/icons'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { cn } from '@/lib/utils'
import { RjsfForm } from '@/services/schema/RjsfForm'
import { useAgentTabStore } from '@/stores/agentTabStore'
import { usePipelineMessageStore } from '@/stores/pipelineMessageStore'
import { useSessionStore } from '@/stores/sessionStore'
import { resolveChatCardIcon } from '@/utils/chatCardIconRegistry'
import type { UIInputFormField } from '@/types/schema'

/**
 * 提取安全的字段数组
 *
 * @param fields - 原始字段定义
 * @returns 类型安全的 UIInputFormField 数组
 */
function extractFields(fields: unknown): UIInputFormField[] {
  if (!Array.isArray(fields)) return []
  return fields.filter(
    (f): f is UIInputFormField =>
      typeof f === 'object' && f !== null && typeof (f as UIInputFormField).name === 'string',
  )
}

type SubmitStatus = 'idle' | 'submitting' | 'success' | 'error' | 'unchanged'

/** 紧凑下拉模式的选项定义（字段 select 的 options 结构） */
interface CompactOption {
  label: string
  value: string
  description?: string
}

/** 当前选中管道标签的 pipeline id（权限模式按管道隔离的 key） */
function useActivePipelineId(): string {
  const activeTabId = useAgentTabStore((s) => s.activeTabId)
  const tabs = useAgentTabStore((s) => s.tabs)
  const activePipelineId = usePipelineMessageStore((s) => s.activePipelineId)
  const sessionId = useSessionStore((s) => s.activeSessionId)
  const tabPipelineId = tabs.find((t) => t.id === activeTabId)?.pipelineRunId
  return tabPipelineId ?? activePipelineId ?? sessionId ?? ''
}

/**
 * 表单交互组件
 *
 * @param props - 组件属性，包含 fields、layout、onSubmit、endpoint 等
 * @returns 动态表单渲染结果
 */
export function FormWidget(props: Record<string, unknown>) {
  const fields = extractFields(props.fields)
  const onSubmit = props.onSubmit as ((data: Record<string, unknown>) => void) | undefined
  const layout = props.layout === 'grid' ? 'double' : 'single'
  const endpoint = props.endpoint as string | undefined
  // 受控模式（宿主注入场景：值跟随宿主 state，变更经 onChange 流出——
  // 与 endpoint 直连互斥使用；如思考强度跟随当前管道标签）
  const onChange = props.onChange as ((data: Record<string, unknown>) => void) | undefined
  const controlledValue = (props.value ?? props.initialValues) as
    | Record<string, unknown>
    | undefined
  const [status, setStatus] = useState<SubmitStatus>('idle')
  const [statusText, setStatusText] = useState('')
  const pipelineId = useActivePipelineId()

  const handleSubmit = async (values: Record<string, unknown>) => {
    if (endpoint) {
      setStatus('submitting')
      setStatusText('提交中…（高风险操作可能弹出审批窗等待确认）')
      try {
        const resp = await fetch(endpoint, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ pipeline_id: pipelineId, ...values }),
        })
        const data = (await resp.json()) as { switched?: boolean; unchanged?: boolean; reason?: string; error?: string }
        if (data.switched) {
          setStatus('success')
          setStatusText('已切换')
        } else if (data.unchanged) {
          setStatus('unchanged')
          setStatusText('当前已是该模式')
        } else {
          setStatus('error')
          setStatusText(data.reason ?? data.error ?? '切换失败')
        }
      } catch {
        setStatus('error')
        setStatusText('请求失败')
      }
      return
    }
    if (onSubmit) {
      await onSubmit(values)
    }
  }

  // 紧凑下拉形态：单 select 字段（endpoint 直连或受控 onChange 二者其一）
  const compactField = fields.length === 1 && fields[0].type === 'select' ? fields[0] : undefined
  const compact = Boolean(compactField && (endpoint || onChange))

  if (compact && compactField) {
    return (
      <CompactSelectToggle
        field={compactField}
        title={props.title as string | undefined}
        icon={props.icon as string | undefined}
        onSelect={handleSubmit}
        onPick={onChange}
        currentValue={
          controlledValue ? (controlledValue[compactField.name] as string | undefined) : undefined
        }
        disabled={props.disabled as boolean | undefined}
        status={status}
        statusText={statusText}
      />
    )
  }

  return (
    <div>
      <RjsfForm
        fields={fields}
        layout={layout}
        title={props.title as string | undefined}
        submitLabel={(props.submitLabel as string) ?? '提交'}
        initialValues={controlledValue}
        onSubmit={handleSubmit}
      />
      {status !== 'idle' && (
        <p className={cn('mt-1 text-xs', statusClass(status))} data-testid="form-widget-status">
          {statusText}
        </p>
      )}
    </div>
  )
}

// ============================================================================
// 决策选择适配器（decision 注册名 → FormWidget 词汇）
// ============================================================================

/** 旧 DecisionWidget 的选项结构 */
interface DecisionOption {
  id: string
  label: string
  description?: string
  disabled?: boolean
  style?: string
  icon?: string
}

function extractDecisionOptions(options: unknown): DecisionOption[] {
  if (!Array.isArray(options)) return []
  return options.filter(
    (o): o is DecisionOption =>
      typeof o === 'object' && o !== null && typeof (o as DecisionOption).id === 'string',
  )
}

/**
 * 决策选择 = 单字段表单（decision 注册名的实现，原 DecisionWidget 已删）
 *
 * options 映射为 radio（单选）/ checkbox（多选）字段，走 RjsfForm 字段模式
 * （无提交按钮），点选即经 onChange 回调——保持旧版「即点即回调」语义。
 *
 * @param props - options/multiple/onDecision/title（与旧 DecisionWidget 同构）
 */
export function DecisionFormAdapter(props: Record<string, unknown>) {
  const options = extractDecisionOptions(props.options)
  const multiple = (props.multiple as boolean) ?? false
  const onDecision = props.onDecision as
    | ((selected: string | string[]) => void)
    | undefined
  const title = props.title as string | undefined

  if (options.length === 0) {
    return (
      <RjsfForm fields={[]} title={title} />
    )
  }

  const field: UIInputFormField = {
    name: 'decision',
    type: multiple ? 'checkbox' : 'radio',
    label: title ?? '决策',
    options: options.map((o) => ({
      label: o.description ? `${o.label}（${o.description}）` : o.label,
      value: o.id,
    })),
  }

  return (
    <RjsfForm
      fields={[field]}
      title={title}
      onChange={(values) => {
        if (!onDecision) return
        const v = values.decision
        onDecision(
          multiple
            ? Array.isArray(v)
              ? v.map(String)
              : []
            : v == null || v === ''
              ? ''
              : String(v),
        )
      }}
    />
  )
}

function statusClass(status: SubmitStatus): string {
  if (status === 'success') return 'text-emerald-600'
  if (status === 'error') return 'text-red-600'
  return 'text-muted-foreground'
}

/**
 * 紧凑下拉选择器（单 select，endpoint 直连或受控 onChange）
 *
 * 图标按钮 + DropdownMenu（label + description + Check），点选即提交/回调。
 * 用于插件声明式选择器（如权限模式切换 / 思考强度跟随管道标签），不占表单布局。
 * icon 为语义字符串（经 chatCardIconRegistry 解析，如 'shield'/'brain'），缺省 shield。
 */
function CompactSelectToggle({
  field,
  title,
  icon,
  onSelect,
  onPick,
  currentValue,
  disabled: disabledProp,
  status,
  statusText,
}: {
  field: UIInputFormField
  title?: string
  icon?: string
  onSelect: (values: Record<string, unknown>) => Promise<void>
  onPick?: (values: Record<string, unknown>) => void
  /** 受控当前值（宿主注入；缺省回退 field.default / 首选项） */
  currentValue?: string
  disabled?: boolean
  status: SubmitStatus
  statusText: string
}) {
  const options = Array.isArray(field.options) ? (field.options as CompactOption[]) : []
  const value = currentValue ?? (field.default as string) ?? options[0]?.value ?? ''
  const current = options.find((o) => o.value === value)
  const submitting = status === 'submitting'
  const disabled = disabledProp || submitting || options.length === 0
  const Icon = resolveChatCardIcon(icon ?? 'shield')

  const handlePick = (value: string) => {
    if (onPick) {
      onPick({ [field.name]: value })
      return
    }
    void onSelect({ [field.name]: value })
  }

  return (
    <div className="flex items-center gap-2">
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <button
            type="button"
            data-testid="compact-select-trigger"
            className={cn(
              'flex h-8 items-center gap-1.5 rounded-lg px-2.5 text-xs font-medium transition-all duration-200',
              'hover:shadow-sm',
              'bg-primary text-primary-foreground hover:bg-primary/90',
              disabled && 'cursor-not-allowed opacity-50',
            )}
            disabled={disabled}
            title={title ?? field.label}
          >
            <Icon className="h-icon-md w-icon-md" />
            <span>{current?.label ?? field.label}</span>
            <ChevronDown className="h-icon-xs w-icon-xs opacity-70" />
          </button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="start" sideOffset={6} className="w-48">
          <DropdownMenuLabel className="text-[11px]">{field.label}</DropdownMenuLabel>
          <DropdownMenuSeparator />
          {options.map((option) => (
            <DropdownMenuItem
              key={option.value}
              onClick={() => handlePick(option.value)}
              className={cn(
                'flex items-center justify-between gap-2',
                option.value === value && 'text-primary',
              )}
            >
              <span className="flex min-w-0 flex-col">
                <span className="text-[13px] font-medium">{option.label}</span>
                {option.description && (
                  <span className="text-muted-foreground text-[11px]">{option.description}</span>
                )}
              </span>
              {option.value === value && <Check className="h-icon-sm w-icon-sm shrink-0" />}
            </DropdownMenuItem>
          ))}
        </DropdownMenuContent>
      </DropdownMenu>
      {status !== 'idle' && (
        <span className={cn('text-xs', statusClass(status))} data-testid="form-widget-status">
          {statusText}
        </span>
      )}
    </div>
  )
}
