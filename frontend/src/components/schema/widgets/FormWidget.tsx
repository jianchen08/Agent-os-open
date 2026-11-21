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
 *   session_id: 当前会话, ...extraBody, ...values} 到该端点，展示提交中/成功/
 *   失败状态。pipeline_id 跟随当前选中的管道标签（agentTabStore.activeTabId →
 *   pipelineRunId，回退 store 级 activePipelineId），仅作附加上文；权限模式等
 *   会话级设置以 session_id 为键（BUG-15 裁定，同会话内 pipeline_id 会轮换）。
 *   声明 createSession=true 时，无激活管道提供「新建会话」入口（会话创建共享
 *   流，创建即激活新主管道，pipeline_id 注入随之解析）。
 * - props.dataUri：GET 初值（yaml 文本自动解析）；提交 PUT/POST 回写
 *   （dataFormat=yaml 时序列化为 {yaml} 体，对齐 agent 配置写回协议）。
 * - props.onSubmit：显式 JS 回调（宿主注入场景；modal 模式提交成功自动关闭）。
 * - props.value + props.onChange：受控模式（宿主注入当前值 + 变更回调，
 *   值跟随宿主 state，如思考强度随管道标签变化）。
 *
 * @module FormWidget
 */

import { useCallback, useEffect, useId, useRef, useState } from 'react'
import { Check, ChevronDown } from '@/assets/icons'
import { SessionEditModal, type SessionFormOptions } from '@/components/session/SessionEditModal'
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { toast } from '@/components/ui/sonner'
import { useSessionsQuery } from '@/hooks/queries/useSessionsQuery'
import { useActivePipelineId, useActiveSessionId } from '@/hooks/useActiveExecutionContext'
import { useAndroidBackCloser } from '@/hooks/useAndroidBackCloser'
import { cn } from '@/lib/utils'
import apiClient from '@/services/api/client'
import { emitFormEvent } from '@/services/schema/formEventBus'
import { RjsfForm } from '@/services/schema/RjsfForm'
import { parseYamlObject, serializeYaml } from '@/services/schema/yaml'
// 紧凑下拉定位策略（对齐 clamp，P0-2）：常量与纯函数单源独立文件
// （react-refresh 禁组件文件新增非组件导出）
import { createSessionWithProject } from '@/services/sessionCreation'
import { openWorkspacePanelByPath } from '@/services/workspacePanelOpener'
import { resolveChatCardIcon } from '@/utils/chatCardIconRegistry'
import { mergeFormValues } from '@/utils/configFormFields'
import {
  COMPACT_SELECT_VIEWPORT_MARGIN,
  compactSelectAlign,
} from './compactSelectLayout'
import { compactTriggerLabel, triggerVisibleLabel } from './compactTriggerLabels'
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

/** 响应取 ETag：优先响应头，回退响应体 etag 字段（对齐 pluginConfig extractEtag 语义） */
function responseEtag(headers: unknown, body: unknown): string {
  const headerEtag = (headers as Record<string, unknown> | undefined)?.etag
  if (typeof headerEtag === 'string' && headerEtag.length > 0) return headerEtag
  const bodyEtag = (body as { etag?: unknown } | undefined)?.etag
  return typeof bodyEtag === 'string' ? bodyEtag : ''
}

/** 紧凑下拉模式的选项定义（字段 select 的 options 结构） */
interface CompactOption {
  label: string
  value: string
  description?: string
  /** 选项图标（声明原文透传，如 emoji '💻'；触发器图标另经 props.icon 走语义图标注册表） */
  icon?: string
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
  const sessionId = useActiveSessionId()
  // 会话列表（TanStack Query 缓存）：取当前会话的隔离形态（isolationMode/
  // workspaceMode），供权限模式选择器如实显示隔离免审批默认（生效档=旁路，
  // OBS-R255-1：免审批默认由旁路档承载，未显式选择仍区别于显式选旁路）
  const { data: sessions } = useSessionsQuery()
  // 回读端点（可选）：挂载时 + 提交成功后 GET 查询当前值并刷新选择器显示。
  // 用于"切换端点的当前值不在表单初值里"的声明式选择器（如权限模式——
  // 值存后端 _PERMISSION_MODES 表，前端无初值来源，不回读则恒显示默认档）。
  const readbackUri = props.readbackUri as string | undefined
  const [backValue, setBackValue] = useState<string | undefined>(undefined)

  const readBack = useCallback(async () => {
    // 权限模式等会话级设置的键位是 session_id（BUG-15），pipeline_id 只是
    // 附带上文——二者有其一即可回读；都缺则无键可查。
    if (!readbackUri || (!pipelineId && !sessionId)) return
    try {
      const params = new URLSearchParams()
      if (pipelineId) params.set('pipeline_id', pipelineId)
      if (sessionId) params.set('session_id', sessionId)
      const resp = await apiClient.get<{ mode?: string; explicit?: boolean; error?: string }>(
        `${readbackUri}?${params.toString()}`,
      )
      const data = resp.data
      if (data.error) throw new Error(data.error)
      // 仅显式选择回填显示值：explicit=false（用户未选择过）时清空——显示值
      // 由会话隔离形态推导（隔离/worktree → 免审批默认），同时消除切换管道
      // 后残留旧管道显示值的竞态
      setBackValue(
        data.explicit && typeof data.mode === 'string' && data.mode !== ''
          ? data.mode
          : undefined,
      )
    } catch (err) {
      // 回读失败保留占位显示（currentValue 缺省回退 field.default），不阻断交互
      console.warn('[FormWidget] readback failed:', readbackUri, err instanceof Error ? err.message : err)
    }
  }, [readbackUri, pipelineId, sessionId])

  // ── datasource 模式（widget 化 T12）──
  const fieldsUri = props.fieldsUri as string | undefined
  const dataUri = props.dataUri as string | undefined
  const dataFormat = (props.dataFormat as 'json' | 'yaml') ?? 'json'
  const submitMethod = (props.submitMethod as 'PUT' | 'POST') ?? 'PUT'
  const extraBody = props.extraBody as Record<string, unknown> | undefined
  const onSaved = props.onSaved as (() => void) | undefined
  const [dsFields, setDsFields] = useState<unknown[] | null>(null)
  const [dsValues, setDsValues] = useState<Record<string, unknown> | null>(null)
  // 乐观锁 etag：GET dataUri 时捕获（响应头优先/信封 etag 回退），PUT 经 body
  // if_match 回传（/ext 写面约定，同 pluginConfig savePluginConfigFile——
  // agent_manager 写面缺失/不匹配恒 409）
  const dsEtagRef = useRef<string>('')
  // GET dataUri 的原始配置（yaml 解析后/json 原对象）：提交按字段路径 merge 写回，
  // 未声明键原样保留（契约同 utils/configFormFields mergeFormValues——BUG-18：
  // 曾 PUT serializeYaml(values) 只剩表单字段值，agent 配置往返丢 category/
  // plugins/static_vars 等全部未声明顶层键）
  const dsOriginalRef = useRef<Record<string, unknown> | null>(null)
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
          if (!cancelled) dsEtagRef.current = responseEtag(resp.headers, d)
          if (dataFormat === 'yaml') {
            const rec = d as { yaml?: unknown }
            const text = typeof d === 'string' ? d : typeof rec?.yaml === 'string' ? rec.yaml : ''
            const parsed = parseYamlObject(text)
            if (!cancelled) {
              dsOriginalRef.current = parsed
              setDsValues(parsed)
            }
          } else {
            const obj =
              d && typeof d === 'object' && !Array.isArray(d) ? (d as Record<string, unknown>) : {}
            if (!cancelled) {
              dsOriginalRef.current = obj
              setDsValues(obj)
            }
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

  useEffect(() => {
    // 挂载时回读一次当前值：保证选择器显示真实状态而非默认占位
    void readBack()
  }, [readBack])

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
  // 缺口 G3：事件联动——提交成功 emit `{eventName}`(payload=表单值)，
  // 失败 emit `{eventName}:failed`(payload={error, values})；声明传 eventName
  const eventName = props.eventName as string | undefined
  // 声明 createSession：无激活管道（提交必缺 pipeline_id）时提供「新建会话」
  // 入口——模态框复用会话创建共享流，创建即激活新主管道，pipeline_id 注入
  // 随之解析（useActivePipelineId 响应式），提示条自行消失
  const createSessionEnabled = props.createSession === true && Boolean(endpoint)
  const [sessionModalOpen, setSessionModalOpen] = useState(false)
  const [isCreatingSession, setIsCreatingSession] = useState(false)
  // modal 壳的表单元素 id：主操作提交钮钉在弹窗 footer（高表单弹层主操作
  // 视口可达，R14），经 form 属性原生关联表单——校验与 onSubmit 流不变
  const reactId = useId()
  const modalFormId = props.modal ? `formwidget-form-${reactId.replace(/:/g, '')}` : undefined

  const handleSessionModalSave = useCallback(
    async (
      _sessionId: string | null,
      title: string,
      agentId: string | null,
      options?: SessionFormOptions,
    ) => {
      setIsCreatingSession(true)
      try {
        await createSessionWithProject(title || undefined, agentId, options, 'FormWidget')
        setSessionModalOpen(false)
      } catch (err) {
        toast.error('创建会话失败', {
          description: err instanceof Error ? err.message : String(err),
        })
      } finally {
        setIsCreatingSession(false)
      }
    },
    [],
  )
  const emitSuccess = (values: Record<string, unknown>) => {
    if (eventName) emitFormEvent(eventName, values)
  }
  const emitFailure = (error: unknown, values: Record<string, unknown>) => {
    if (eventName) emitFormEvent(`${eventName}:failed`, { error: error instanceof Error ? error.message : String(error), values })
  }

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
        // BUG-18：表单值按字段路径 merge 到 GET 原配置上（未声明键原样保留，
        // 契约同 PluginConfigEditor mergeFormValues），不再裸序列化表单值
        const merged = dsOriginalRef.current
          ? mergeFormValues(dsOriginalRef.current, fields, values)
          : values
        // 乐观锁：GET 捕获的 etag 经 body if_match 回传（lock 置尾，声明的
        // extraBody 有显式 if_match 时以实读指纹为准）
        const lock = dsEtagRef.current ? { if_match: dsEtagRef.current } : {}
        const body =
          dataFormat === 'yaml'
            ? { yaml: serializeYaml(merged), ...lock }
            : { ...merged, ...(extraBody ?? {}), ...lock }
        const resp = await apiClient({ method: submitMethod, url: dataUri, data: body })
        // 成功写回换新指纹（响应头/响应体），连续保存不用过期 etag 误触 409
        dsEtagRef.current = responseEtag(resp.headers, resp.data)
        setStatus('success')
        setStatusText(successText ?? '已保存')
        onSaved?.()
        runSuccessAction(successAction)
        emitSuccess(values)
      } catch (err) {
        setStatus('error')
        // 409 = 乐观锁冲突：显式指引（禁止静默，也不得显示成功）；failureText
        // 让位冲突语义—— remedy 是刷新重读而非重试覆盖
        const conflict =
          (err as { response?: { status?: number } } | undefined)?.response?.status === 409
        setStatusText(
          conflict
            ? '配置已被他人修改，请刷新后重试'
            : failureText ?? (err instanceof Error ? err.message : '保存失败'),
        )
        emitFailure(err, values)
      }
      return
    }
    if (endpoint) {
      setStatus('submitting')
      setStatusText('提交中…（高风险操作可能弹出审批窗等待确认）')
      // 载荷优先级：表单值 > extraBody > 注入值；pipeline_id 为空（未选/空串）
      // 时回落注入的当前激活管道——「留空则提交给当前激活管道」字段契约的落实，
      // 空表单值不得抹掉注入坐标
      const body: Record<string, unknown> = {
        session_id: sessionId ?? '',
        ...(extraBody ?? {}),
        ...values,
      }
      if (!body.pipeline_id) body.pipeline_id = pipelineId
      // createSession 表单以管道为作用对象：无任何管道坐标时提交必被后端拒——
      // 本地拦截并给行动指引，不发必败请求（失败必须可见，禁止静默）
      if (createSessionEnabled && !body.pipeline_id) {
        const noPipelineMsg = '当前没有激活管道：可在「目标管道」选择已有管道，或新建会话后再提交'
        setStatus('error')
        setStatusText(noPipelineMsg)
        emitFailure(new Error(noPipelineMsg), values)
        return
      }
      try {
        // endpoint 直连统一走 apiClient（认证头注入 + 401 刷新链，auth:user
        // 的 /ext 端点缺头会被内核 401 拒绝）；4xx/5xx 由拦截器 reject
        const resp = await apiClient.post<{
          switched?: boolean
          unchanged?: boolean
          reason?: string
          error?: string
          message?: string
        }>(endpoint, body)
        const data = resp.data
        // endpoint 响应协议：error/reason = 失败；unchanged=true = 无变更（仅
        // 权限模式等切换端点用）；其余一律视为成功（通用表单端点的成功
        // 响应体是创建/更新对象，无 switched 字段）
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
          emitSuccess(values)
          void readBack()
        }
      } catch (err) {
        setStatus('error')
        // 失败必须携带后端真实原因（拦截器 reject 的 Error.message 已是统一
        // 错误信封的业务文案）；仅非 Error 形状才退化笼统文案
        setStatusText(failureText ?? (err instanceof Error ? err.message : '请求失败'))
        emitFailure(err, values)
      }
      return
    }
    if (onSubmit) {
      // submitting 上浮到 FormWidget：modal 壳 footer 提交钮消费该态做禁用/
      // 提交中文案（原表单内钮的 loading 由 RjsfForm 内部态承载，钮迁 footer
      // 后宿主态必须可见）；提交中文案由 footer 钮承载，statusText 留空
      setStatus('submitting')
      setStatusText('')
      try {
        await onSubmit(values)
        setStatus('success')
        setStatusText(successText ?? '已提交')
        onSaved?.()
        runSuccessAction(successAction)
        emitSuccess(values)
      } catch (err) {
        setStatus('error')
        setStatusText(failureText ?? (err instanceof Error ? err.message : String(err)))
        toast.error('提交失败', {
          description: err instanceof Error ? err.message : String(err),
        })
        emitFailure(err, values)
      }
    }
  }

  // 紧凑下拉形态：单 select 字段（endpoint 直连或受控 onChange 二者其一）。
  // datasource/modal 模式不适用紧凑形态。
  const compactField = fields.length === 1 && fields[0].type === 'select' ? fields[0] : undefined
  const compact = Boolean(
    compactField && (endpoint || onChange) && !fieldsUri && !dataUri && !props.modal,
  )
  // 紧凑形态的声明短写：字段 label 缺省回退 props.title（二者同值时只写一处）
  const compactSetting = compactField
    ? ({ ...compactField, label: compactField.label ?? (props.title as string | undefined) ?? '' })
    : undefined

  // ── modal 壳模式（widget 化 T12）：受控 open/onClose 或 trigger 按钮 ──
  const modalCfg = props.modal as { trigger?: string; title?: string } | undefined

  // 提交状态反馈：非 modal 态落在表单体末尾；modal 态由 ModalShell 渲染在
  // footer（主操作旁）——高表单弹层提交钮在 footer，反馈若留在表单体末尾
  // 会落在滚动区外不可见（R14）
  const statusEl =
    status !== 'idle' ? (
      <p className={cn('text-xs', statusClass(status))} data-testid="form-widget-status">
        {statusText}
      </p>
    ) : null

  const formBody = (
    <div>
      {dsError && (
        <p className="text-status-error mb-2 text-xs" role="alert">
          {dsError}
        </p>
      )}
      {createSessionEnabled && !pipelineId && (
        <p className="text-muted-foreground mb-2 text-xs">
          当前没有激活管道：可在「目标管道」选择已有管道，或
          <button
            type="button"
            onClick={() => setSessionModalOpen(true)}
            className="text-primary mx-1 underline-offset-2 hover:underline"
          >
            新建会话
          </button>
          后再提交
        </p>
      )}
      {!dsReady ? (
        <p className="text-muted-foreground py-4 text-center text-sm">加载表单数据…</p>
      ) : (
        <>
          {/* key=dataUri 就绪态：异步初值到达后重挂载，使 RjsfForm 捕获 initialValues。
              modal 态：formId 落到 form 元素 + 收起表单内提交钮（主操作钉 footer） */}
          <RjsfForm
            key={String(dsReady)}
            fields={fields}
            layout={layout}
            title={props.title as string | undefined}
            submitLabel={(props.submitLabel as string) ?? '提交'}
            initialValues={effectiveInitial}
            onSubmit={handleSubmit}
            disabled={props.disabled as boolean | undefined}
            formId={modalFormId}
            hideSubmit={modalFormId !== undefined}
          />
        </>
      )}
      {!modalCfg && statusEl}
    </div>
  )

  if (modalCfg) {
    return (
      <ModalShell
        trigger={modalCfg.trigger}
        title={modalCfg.title}
        open={props.open as boolean | undefined}
        onClose={props.onClose as (() => void) | undefined}
        closeOnSuccess={status === 'success'}
        formId={modalFormId}
        submitLabel={(props.submitLabel as string) ?? '提交'}
        submitDisabled={!dsReady || fields.length === 0 || status === 'submitting' || props.disabled === true}
        submitting={status === 'submitting'}
        statusEl={statusEl}
      >
        {formBody}
      </ModalShell>
    )
  }

  if (compact && compactField) {
    // 会话隔离形态（隔离容器 / worktree 副本）且未显式选择权限档：后端生效
    // 语义为免审批默认（旁路档承载，OBS-R255-1），显式选择可覆盖——选择器
    // 如实显示该默认而非 default 档；不显示「旁路」标签是为了保留「未显式
    // 选择」（explicit=false）与「显式选旁路」的可辨别性（后端 explicit 协议同源）
    const activeSession = sessions?.find((s) => s.id === sessionId)
    const isolatedDefaultFreePass =
      activeSession?.isolationMode === 'isolated' || activeSession?.workspaceMode === 'worktree'
    return (
      <CompactSelectToggle
        field={compactSetting ?? compactField}
        title={props.title as string | undefined}
        icon={props.icon as string | undefined}
        onSelect={handleSubmit}
        onPick={onChange}
        currentValue={
          backValue ??
          (effectiveInitial
            ? (effectiveInitial[compactField.name] as string | undefined)
            : undefined)
        }
        unselectedLabel={
          isolatedDefaultFreePass && !backValue && !effectiveInitial
            ? '免审批（隔离默认）'
            : undefined
        }
        disabled={props.disabled as boolean | undefined}
        status={status}
        statusText={statusText}
      />
    )
  }

  return (
    <>
      {formBody}
      {createSessionEnabled && (
        <SessionEditModal
          mode="create"
          isOpen={sessionModalOpen}
          session={null}
          onClose={() => setSessionModalOpen(false)}
          onSave={handleSessionModalSave}
          isSaving={isCreatingSession}
        />
      )}
    </>
  )
}

/** modal 壳：受控 open/onClose（缺省 trigger 按钮自开关），成功态自动关闭。
 *  主操作提交钮钉在 footer（sticky）：高表单弹层内容滚、footer 常驻，主操作
 *  与提交反馈初始即在视口内（R14）；提交钮经 form 属性关联表单原生提交，
 *  校验与 onSubmit 流不变。 */
function ModalShell({
  trigger,
  title,
  open: controlledOpen,
  onClose,
  closeOnSuccess,
  formId,
  submitLabel = '提交',
  submitDisabled = false,
  submitting = false,
  statusEl,
  children,
}: {
  trigger?: string
  title?: string
  open?: boolean
  onClose?: () => void
  closeOnSuccess?: boolean
  /** 表单元素 id（FormWidget modal 态必有）：footer 提交钮 form 关联的目标 */
  formId?: string
  submitLabel?: string
  submitDisabled?: boolean
  submitting?: boolean
  statusEl?: React.ReactNode
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
    // HACK: onClose 是调用方每次渲染的自由闭包（非稳定引用），列入依赖会
    // 重触发本 effect；effect 只关心提交成功边沿（closeOnSuccess 翻真），
    // 关闭动作本身无重入风险。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [closeOnSuccess, open])

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
        {/* 宽度自适应（R14 追加）：DialogContent 内联 width 缺省写死 500px 且
            压过类侧 max-w-*（max-w-2xl 是死类）——桌面宽视口按 min(90vw,56rem)
            放大（agent 配置/长提示词表单不再挤窄框），窄视口由 90vw 收口 */}
        <DialogContent width="min(90vw, 56rem)">
          <DialogHeader>
            <DialogTitle>{title ?? '填写表单'}</DialogTitle>
          </DialogHeader>
          <div className="max-h-[65vh] overflow-y-auto p-6 pt-2">{children}</div>
          {/* sticky 底沿（同 ui/dialog DialogFooter R13 形态）：弹窗根溢出
              （dvh 随键盘收缩等）时 footer 钉住；不溢出时 sticky 不生效 */}
          <div className="bg-popover sticky bottom-0 z-10 flex items-center justify-end gap-2 rounded-b-[inherit] border-t px-6 pb-4 pt-3">
            <div className="min-w-0 flex-1">{statusEl}</div>
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
            {formId && (
              <button
                type="submit"
                form={formId}
                disabled={submitDisabled}
                data-testid="formwidget-modal-submit"
                className="bg-primary text-primary-foreground hover:bg-primary/90 rounded-md px-3 py-1.5 text-sm disabled:cursor-not-allowed disabled:opacity-50"
              >
                {submitting ? '提交中…' : submitLabel}
              </button>
            )}
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
 * （无提交按钮），点选即经 onChange 回调——「即点即回调」语义。
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
  if (status === 'success') return 'text-status-success'
  if (status === 'error') return 'text-status-error'
  return 'text-muted-foreground'
}

/**
 * 紧凑下拉选择器（单 select，endpoint 直连或受控 onChange）
 *
 * 图标按钮 + DropdownMenu（label + description + Check），点选即提交/回调。
 * 用于插件声明式选择器（如权限模式切换 / 思考强度跟随管道标签），不占表单布局。
 * icon 为语义字符串（经 chatCardIconRegistry 解析，如 'shield'/'brain'），缺省 shield。
 *
 * 两种触发器形态（按声明 icon 是否在场判别，渲染层零 id 感知）：
 * - icon 缺省 → 文字形态：可见文案 = 裸当前值（如「高」/「旁路（跳过审批）」）
 *   ——输入条宽度有限，设置名前缀不进可见文案。
 * - icon 在场 → icon-only：触发器只留声明图标 + 下拉箭头（用户原话"不要写
 *   思考强度4个字"），当前值只在展开菜单勾选态可见；设置名退到 title 悬停。
 *
 * 自标识由可访问名承载（BUG-6，读屏与 GUI 脚本按带前缀的可访问名定位）：
 * 有当前值（命中选项或隔离免审批显示档）→「`${设置名}：${当前值}`」；无值可示
 * （未选择且无显示档，当前值兜底落在设置名本身）→ 只留设置名，不产生复读。
 * 设置名取 props.title，回退字段 label。
 *
 * 声明短写（本形态专用，2026-09-21 裁定）：单 select 选择器的 props 只需
 * `title` + `fields`（+ endpoint/readbackUri 等行为键）。字段 label 与 title 同值时
 * 只写 title；widget 级 `title` 不写（ContributionRegistry 归一化时丢弃，是死字段）；
 * `description` 不写（本形态无 UI 载体——选项说明写在 options[].description）。
 */
function CompactSelectToggle({
  field,
  title,
  icon,
  onSelect,
  onPick,
  currentValue,
  unselectedLabel,
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
  /** 无当前值时的显示档（如隔离会话免审批默认）：触发器显示该文案，
   *  菜单不勾选任何档位——该状态不是选项之一，是"未显式选择"的生效默认 */
  unselectedLabel?: string
  disabled?: boolean
  status: SubmitStatus
  statusText: string
}) {
  const options = Array.isArray(field.options) ? (field.options as CompactOption[]) : []
  const value =
    currentValue ?? (unselectedLabel ? '' : (field.default as string) ?? options[0]?.value ?? '')
  const current = options.find((o) => o.value === value)
  const submitting = status === 'submitting'
  const disabled = disabledProp || submitting || options.length === 0
  const Icon = resolveChatCardIcon(icon ?? 'shield')
  // 视口 clamp（P0-2）：展开瞬间量触发器右缘定对齐——靠右触发器翻转向左展开，
  // 不依赖 radix collision 的环境差异（MuMu WebView 实测 align=start 溢出右缘）
  const triggerRef = useRef<HTMLButtonElement>(null)
  const [menuOpen, setMenuOpen] = useState(false)
  const [align, setAlign] = useState<'start' | 'end'>('start')
  const handleOpenChange = (next: boolean) => {
    if (next && triggerRef.current && typeof window !== 'undefined') {
      setAlign(compactSelectAlign(window.innerWidth, triggerRef.current.getBoundingClientRect().right))
    }
    setMenuOpen(next)
  }
  // Android back 优先关浮层（P1-8，MuMu 实测 shots/05 场景：思考强度浮窗打开
  // 按 back 直接回退/退出应用）：菜单展开期间注册关闭器，back 一按先关菜单
  // 并消费该按；收起即注销，不吞后续 back
  useAndroidBackCloser(menuOpen, () => setMenuOpen(false))
  // 触发器自标识（BUG-6）：只显示裸值（如「高」）会与相邻权限档位混淆——
  // GUI/读屏按「高」找权限等级会误中思考强度。自标识由可访问名承载
  // （「思考强度：高」），文字形态可见文案只留裸值以省输入条宽度（icon-only
  // 形态可见文案整体退场，见组件 docblock）。文字可见文案按 R10③
  // 精简（triggerVisibleLabel 去括注：桌面「默认（命中规则才确认）」→「默认」、
  // 窄屏（<sm）再收口到 ≤2 字（compactTriggerLabel）），全称由可访问名与菜单承载。
  const settingLabel = title ?? field.label
  const currentLabel = current?.label ?? unselectedLabel ?? field.label ?? ''
  const visibleLabel = triggerVisibleLabel(currentLabel)
  const compactLabel = compactTriggerLabel(currentLabel)
  // icon 在场 → icon-only（机制判别见组件 docblock）；icon 缺省维持文字形态
  const iconOnly = Boolean(icon)
  const accessibleName =
    current || unselectedLabel
      ? [settingLabel, currentLabel].filter(Boolean).join('：')
      : (settingLabel ?? currentLabel) || ''

  const handlePick = (value: string) => {
    if (onPick) {
      onPick({ [field.name]: value })
      return
    }
    void onSelect({ [field.name]: value })
  }

  return (
    // min-w-0 双落点（根 div + 触发按钮）：truncate 的 nowrap 会把 span 的
    // 最小内容宽钉在全标签宽，min-width:auto 的祖先链上任一环不放行收缩，
    // 窄视口下整颗 chip 就以自然宽溢出容器（实测压过发送键 27px）
    <div className="flex min-w-0 items-center gap-2">
      <DropdownMenu open={menuOpen} onOpenChange={handleOpenChange}>
        <DropdownMenuTrigger asChild>
          <button
            ref={triggerRef}
            type="button"
            data-testid="compact-select-trigger"
            className={cn(
              'flex h-8 min-w-0 items-center gap-1 rounded-lg px-2 text-xs font-medium transition-all duration-200 sm:gap-1.5 sm:px-2.5',
              'hover:shadow-sm',
              'bg-primary text-primary-foreground hover:bg-primary/90',
              disabled && 'cursor-not-allowed opacity-50',
            )}
            disabled={disabled}
            aria-label={accessibleName || undefined}
            title={title ?? field.label}
          >
            <Icon className="h-icon-md w-icon-md shrink-0" />
            {!iconOnly && (
              <>
                <span className="min-w-0 truncate sm:hidden" aria-hidden="true">
                  {compactLabel}
                </span>
                <span className="hidden min-w-0 truncate sm:inline">{visibleLabel}</span>
              </>
            )}
            <ChevronDown className="h-icon-xs w-icon-xs shrink-0 opacity-70" />
          </button>
        </DropdownMenuTrigger>
        <DropdownMenuContent
          align={align}
          collisionPadding={COMPACT_SELECT_VIEWPORT_MARGIN}
          sideOffset={6}
          className="max-h-[min(24rem,var(--radix-dropdown-menu-content-available-height))] w-48 overflow-y-auto"
        >
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
                <span className="flex items-center gap-1.5 text-[13px] font-medium">
                  {option.icon && <span aria-hidden="true">{option.icon}</span>}
                  {option.label}
                </span>
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
