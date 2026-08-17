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
 * 3. datasource 模式（widget 化 T12）：fieldsUri 拉字段声明 + dataUri 拉初值/
 *    写回（吸收原 SchemaFormEmbed：GET/PUT agent 配置 yaml）。
 * 4. modal 壳模式（widget 化 T12）：modal 声明或受控 open/onClose 包 Dialog
 *    （吸收原 CreateTaskModal：提交成功自动关闭 + onSaved 回调）。
 *
 * 提交/受控模式（endpoint 与受控互斥）：
 * - props.endpoint（声明 JSON 可传字符串）：POST {pipeline_id: 当前选中管道,
 *   ...extraBody, ...values} 到该端点，展示提交中/成功/失败状态。pipeline_id
 *   跟随当前选中的管道标签（agentTabStore.activeTabId → pipelineRunId，回退
 *   store 级 activePipelineId / sessionId）——权限模式按管道隔离，切换标签即切换目标。
 * - props.dataUri：GET 初值（yaml 文本自动解析）；提交 PUT/POST 回写
 *   （dataFormat=yaml 时序列化为 {yaml} 体，对齐 agent 配置写回协议）。
 * - props.onSubmit：显式 JS 回调（宿主注入场景；modal 模式提交成功自动关闭）。
 * - props.value + props.onChange：受控模式（宿主注入当前值 + 变更回调，
 *   值跟随宿主 state，如思考强度随管道标签变化）。
 *
 * @module FormWidget
 */

import { useEffect, useState } from 'react'
import { Check, ChevronDown } from '@/assets/icons'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import apiClient from '@/services/api/client'
import { RjsfForm } from '@/services/schema/RjsfForm'
import { parseYamlObject, serializeYaml } from '@/services/schema/yaml'
import { cn } from '@/lib/utils'
import { useAgentTabStore } from '@/stores/agentTabStore'
import { usePipelineMessageStore } from '@/stores/pipelineMessageStore'
import { useSessionStore } from '@/stores/sessionStore'
import { toast } from '@/components/ui/sonner'
import { resolveChatCardIcon } from '@/utils/chatCardIconRegistry'
import { openWorkspacePanelByPath } from '@/services/workspacePanelOpener'
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
 * @param props - 组件属性，包含 fields、layout、onSubmit、endpoint、
 *   fieldsUri/dataUri（datasource 模式）、modal/open/onClose（modal 壳）等
 * @returns 动态表单渲染结果
 */
export function FormWidget(props: Record<string, unknown>) {
  const onSubmit = props.onSubmit as ((data: Record<string, unknown>) => void) | undefined
  const layout = props.layout === 'grid' ? 'double' : 'single'
  const endpoint = props.endpoint as string | undefined
  // 受控模式（宿主注入场景：值跟随宿主 state，变更经 onChange 流出——
  // 与 endpoint 直连互斥使用；如思考强度跟随当前管道标签）
  const onChange = props.onChange as ((data: Record<string, unknown>) => void) | undefined
  const pipelineId = useActivePipelineId()

  // ── datasource 模式（widget 化 T12）──
  const fieldsUri = props.fieldsUri as string | undefined
  const dataUri = props.dataUri as string | undefined
  const dataFormat = (props.dataFormat as 'json' | 'yaml') ?? 'json'
  const submitMethod = (props.submitMethod as 'PUT' | 'POST') ?? 'PUT'
  const extraBody = props.extraBody as Record<string, unknown> | undefined
  const onSaved = props.onSaved as (() => void) | undefined
  const [dsFields, setDsFields] = useState<unknown[] | null>(null)
  const [dsValues, setDsValues] = useState<Record<string, unknown> | null>(null)
  const [dsReady, setDsReady] = useState(!fieldsUri && !dataUri)
  const [dsError, setDsError] = useState<string | null>(null)
  /** successAction.reload 触发 datasource 重拉（须在 effect 依赖之前声明） */
  const [reloadKey, setReloadKey] = useState(0)

  useEffect(() => {
    if (!fieldsUri && !dataUri) return
    let cancelled = false
    setDsReady(false)
    setDsError(null)
    const jobs: Promise<unknown>[] = []
    if (fieldsUri) {
      jobs.push(
        apiClient.get(fieldsUri).then((resp) => {
          const d = resp.data
          const f = Array.isArray(d)
            ? d
            : d && typeof d === 'object' && Array.isArray((d as { fields?: unknown[] }).fields)
              ? (d as { fields: unknown[] }).fields
              : null
          if (f === null) throw new Error('fieldsUri 响应不含 fields 数组')
          if (!cancelled) setDsFields(f)
        }),
      )
    }
    if (dataUri) {
      jobs.push(
        apiClient.get(dataUri).then((resp) => {
          const d: unknown = resp.data
          if (dataFormat === 'yaml') {
            const rec = d as { yaml?: unknown }
            const text = typeof d === 'string' ? d : typeof rec?.yaml === 'string' ? rec.yaml : ''
            if (!cancelled) setDsValues(parseYamlObject(text))
          } else {
            if (!cancelled) setDsValues(
              d && typeof d === 'object' && !Array.isArray(d)
                ? (d as Record<string, unknown>)
                : {},
            )
          }
        }),
      )
    }
    Promise.all(jobs)
      .then(() => {
        if (!cancelled) setDsReady(true)
      })
      .catch((err: unknown) => {
        if (cancelled) return
        setDsError(err instanceof Error ? err.message : '数据源加载失败')
        setDsReady(true)
      })
    return () => {
      cancelled = true
    }
  }, [fieldsUri, dataUri, dataFormat, reloadKey])

  const fields = extractFields(dsFields ?? props.fields)
  const controlledValue = (props.value ?? props.initialValues) as
    | Record<string, unknown>
    | undefined
  const effectiveInitial = dsValues ?? controlledValue
  const [status, setStatus] = useState<SubmitStatus>('idle')
  const [statusText, setStatusText] = useState('')
  // 缺口 G1：声明化反馈文案 + 成功动作（successText/failureText/successAction
  // 均可经 ui_schema.widgets.props 声明 JSON 传递）
  const successText = props.successText as string | undefined
  const failureText = props.failureText as string | undefined
  const successAction = props.successAction as
    | { type: 'open_panel'; path: string }
    | { type: 'reload' }
    | undefined

  const runSuccessAction = (action: typeof successAction) => {
    if (!action) return
    if (action.type === 'open_panel') {
      openWorkspacePanelByPath(action.path)
    } else if (action.type === 'reload') {
      setReloadKey((k) => k + 1)
    }
  }

  const handleSubmit = async (values: Record<string, unknown>) => {
    // datasource 写回：dataUri PUT/POST（yaml 序列化为 {yaml} 体）
    if (dataUri) {
      setStatus('submitting')
      setStatusText('保存中…')
      try {
        const body =
          dataFormat === 'yaml'
            ? { yaml: serializeYaml(values) }
            : { ...values, ...(extraBody ?? {}) }
        await apiClient({ method: submitMethod, url: dataUri, data: body })
        setStatus('success')
        setStatusText(successText ?? '已保存')
        onSaved?.()
        runSuccessAction(successAction)
      } catch (err) {
        setStatus('error')
        setStatusText(failureText ?? (err instanceof Error ? err.message : '保存失败'))
      }
      return
    }
    if (endpoint) {
      setStatus('submitting')
      setStatusText('提交中…（高风险操作可能弹出审批窗等待确认）')
      try {
        const resp = await fetch(endpoint, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ pipeline_id: pipelineId, ...(extraBody ?? {}), ...values }),
        })
        const data = (await resp.json()) as {
          switched?: boolean
          unchanged?: boolean
          reason?: string
          error?: string
          message?: string
        }
        // endpoint 响应协议：error/reason = 失败；unchanged=true = 无变更（仅
        // 权限模式等切换端点用）；其余一律视为成功（通用表单端点返回创建对象等，
        // 旧实现把非 switched 响应误判为失败是该模式的既有缺陷）
        if (data.error || data.reason) {
          setStatus('error')
          setStatusText(failureText ?? data.reason ?? data.error ?? '提交失败')
        } else if (data.unchanged === true) {
          setStatus('unchanged')
          setStatusText(successText ?? data.message ?? '当前已是该模式')
        } else {
          setStatus('success')
          setStatusText(successText ?? data.message ?? '已提交')
          onSaved?.()
          runSuccessAction(successAction)
        }
      } catch {
        setStatus('error')
        setStatusText(failureText ?? '请求失败')
      }
      return
    }
    if (onSubmit) {
      try {
        await onSubmit(values)
        setStatus('success')
        setStatusText(successText ?? '已提交')
        onSaved?.()
        runSuccessAction(successAction)
      } catch (err) {
        setStatus('error')
        setStatusText(failureText ?? (err instanceof Error ? err.message : String(err)))
        toast.error('提交失败', {
          description: err instanceof Error ? err.message : String(err),
        })
      }
    }
  }

  // 紧凑下拉形态：单 select 字段（endpoint 直连或受控 onChange 二者其一）。
  // datasource/modal 模式不适用紧凑形态。
  const compactField = fields.length === 1 && fields[0].type === 'select' ? fields[0] : undefined
  const compact = Boolean(
    compactField && (endpoint || onChange) && !fieldsUri && !dataUri && !props.modal,
  )

  const formBody = (
    <div>
      {dsError && (
        <p className="text-status-error mb-2 text-xs" role="alert">
          {dsError}
        </p>
      )}
      {!dsReady ? (
        <p className="text-muted-foreground py-4 text-center text-sm">加载表单数据…</p>
      ) : (
        <>
          {/* key=dataUri 就绪态：异步初值到达后重挂载，使 RjsfForm 捕获 initialValues */}
          <RjsfForm
            key={String(dsReady)}
            fields={fields}
            layout={layout}
            title={props.title as string | undefined}
            submitLabel={(props.submitLabel as string) ?? '提交'}
            initialValues={effectiveInitial}
            onSubmit={handleSubmit}
            disabled={props.disabled as boolean | undefined}
          />
        </>
      )}
      {status !== 'idle' && (
        <p className={cn('mt-1 text-xs', statusClass(status))} data-testid="form-widget-status">
          {statusText}
        </p>
      )}
    </div>
  )

  // ── modal 壳模式（widget 化 T12）：受控 open/onClose 或 trigger 按钮 ──
  const modalCfg = props.modal as { trigger?: string; title?: string } | undefined
  if (modalCfg) {
    return (
      <ModalShell
        trigger={modalCfg.trigger}
        title={modalCfg.title}
        open={props.open as boolean | undefined}
        onClose={props.onClose as (() => void) | undefined}
        closeOnSuccess={status === 'success'}
      >
        {formBody}
      </ModalShell>
    )
  }

  if (compact && compactField) {
    return (
      <CompactSelectToggle
        field={compactField}
        title={props.title as string | undefined}
        icon={props.icon as string | undefined}
        onSelect={handleSubmit}
        onPick={onChange}
        currentValue={
          effectiveInitial ? (effectiveInitial[compactField.name] as string | undefined) : undefined
        }
        disabled={props.disabled as boolean | undefined}
        status={status}
        statusText={statusText}
      />
    )
  }

  return formBody
}

/** modal 壳：受控 open/onClose（缺省 trigger 按钮自开关），成功态自动关闭 */
function ModalShell({
  trigger,
  title,
  open: controlledOpen,
  onClose,
  closeOnSuccess,
  children,
}: {
  trigger?: string
  title?: string
  open?: boolean
  onClose?: () => void
  closeOnSuccess?: boolean
  children: React.ReactNode
}) {
  const [selfOpen, setSelfOpen] = useState(false)
  const isControlled = controlledOpen !== undefined
  const open = isControlled ? controlledOpen : selfOpen

  useEffect(() => {
    if (closeOnSuccess && open) {
      setSelfOpen(false)
      onClose?.()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [closeOnSuccess])

  return (
    <>
      {trigger && !isControlled && (
        <button
          type="button"
          onClick={() => setSelfOpen(true)}
          className="bg-primary text-primary-foreground rounded-md px-3 py-1.5 text-sm"
        >
          {trigger}
        </button>
      )}
      <Dialog
        open={open}
        onOpenChange={(o) => {
          if (!o) {
            setSelfOpen(false)
            onClose?.()
          }
        }}
      >
        <DialogContent className="max-w-2xl">
          <DialogHeader>
            <DialogTitle>{title ?? '填写表单'}</DialogTitle>
          </DialogHeader>
          <div className="max-h-[65vh] overflow-y-auto p-6 pt-2">{children}</div>
          <div className="flex justify-end gap-2 px-6 pb-4">
            <button
              type="button"
              onClick={() => {
                setSelfOpen(false)
                onClose?.()
              }}
              className="border-border text-muted-foreground hover:bg-muted/70 rounded-md border px-3 py-1.5 text-sm"
            >
              取消
            </button>
          </div>
        </DialogContent>
      </Dialog>
    </>
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
