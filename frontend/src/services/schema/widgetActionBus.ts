/**
 * Widget Action Bus —— widget → 宿主/插件的声明制命令分发
 *
 * [来源: docs/decisions/2026-10-10-widget-action-bus.md] datasourceUri 的动作面
 * 对偶：数据进走声明（datasourceUri），动作出走声明（declareWidgetActions 词表 +
 * ui_schema actions 的 actionUri），widget 不再与具体 store/端点绑死。
 *
 * 三件 API：
 * - `useWidgetAction(name)`（组件侧）：返回触发器 `(payload?) => Promise`，
 *   成功 resolve 动作结果（host adapter 返回值或插件响应体），失败 reject——
 *   错误要么处理要么传播，不吞。
 * - `declareWidgetActions(type, actions)`（声明侧）：type 级动作词表进
 *   WidgetRegistry 注册元数据；组件只能发词表内动作（声明即注册）。
 * - `WidgetActionContext`（宿主侧）：DeclaredWidgetLayer 逐 widget 提供，
 *   value 携带当前声明 + 分发函数（本模块构造、层注入）。
 *
 * 分发解析顺序：内置域 host adapter 绑定优先（应用壳经
 * registerWidgetHostAdapter 注册，动作名绑定具体 store 动作——store 留在宿主
 * 壳，widget 层只见动作名），声明 actionUri 动作次之（第三方 widget 主路径：
 * ui_schema.actions 声明 → apiClient 直调插件 /ext 端点，带 Authorization）。
 *
 * 与 formEventBus 划界（不合并）：formEventBus 是 widget↔widget 数据联动
 * （watch 声明）；本模块是 widget→宿主/插件命令分发（词表 + actionUri 白名单
 * 两道治理）。动作成功后的数据刷新复用既有联动语义：actionUri 动作成功即发
 * formEventBus 事件 `{widgetId}:{actionName}`（payload=响应体），watch 该事件
 * 名的声明组件按既有 reload 语义重拉——不新造刷新协议。
 *
 * 白名单 fail-closed：actionUri 只允许落在该插件自己 manifest 声明的
 * http_endpoints 面内（/api/v1/schema 聚合响应的 routes 面，与 ui_schema 同
 * 响应、无二次拉取）；越界 action 项拒绝注册 + console.error，不拆整个
 * widget。capability 通道动作（非 HTTP 形态）不在本面。
 */

import { useCallback, useContext, createContext } from 'react'
import apiClient from '@/services/api/client'
import { emitFormEvent } from '@/services/schema/formEventBus'
import { widgetRegistry } from '@/services/schema/WidgetRegistry'
import type { WidgetDeclaration } from '@/services/schema/ContributionRegistry'
import type { WidgetActionMeta } from '@/services/schema/WidgetRegistry'

/** 内置域 host adapter：动作名 → 宿主壳动作（绑定具体 store 动作，应用壳注册） */
export type WidgetHostAdapter = (payload: unknown, declaration: WidgetDeclaration | undefined) => Promise<unknown> | unknown

/** ui_schema 声明的 actionUri 动作（白名单校验后仅存合法项） */
export interface WidgetActionUriDecl {
  /** 请求 URI（绝对路径，如 /ext/{plugin}/report/reset） */
  uri: string
  /** HTTP method（缺省 POST；声明小写自动归一大写） */
  method: string
  /** 按钮文案等展示提示（display-only，不参与权限面） */
  label?: string
  description?: string
}

/** WidgetActionContext 值：当前 widget 声明 + 分发函数（DeclaredWidgetLayer 注入） */
export interface WidgetActionContextValue {
  /** 当前渲染的 widget 声明（actionUri 与 type 词表解析源） */
  declaration?: WidgetDeclaration
  /** 动作分发：host adapter 优先 → 声明 actionUri；未声明动作 reject */
  dispatch: (actionName: string, payload?: unknown) => Promise<unknown>
}

/** 宿主动作注入 context（DeclaredWidgetLayer 逐 widget Provider 包裹提供） */
export const WidgetActionContext = createContext<WidgetActionContextValue | null>(null)

// ── 内置域 host adapter 注册表（模块级单例）──────────────────

const hostAdapters = new Map<string, WidgetHostAdapter>()

/**
 * 注册内置域 host adapter（应用壳调用）：动作名 → 宿主 store 动作绑定
 *
 * @throws 同名动作重复注册 = 静默遮蔽，显式报错（ADR：同名冲突注册期报错）
 */
export function registerWidgetHostAdapter(actionName: string, adapter: WidgetHostAdapter): void {
  if (hostAdapters.has(actionName)) {
    throw new Error(`内置域动作 "${actionName}" 已有 host adapter 绑定，重复注册即遮蔽（ADR 2026-10-10）`)
  }
  hostAdapters.set(actionName, adapter)
}

/** 是否已绑定内置域 host adapter（声明期同名冲突检测用） */
export function hasWidgetHostAdapter(actionName: string): boolean {
  return hostAdapters.has(actionName)
}

/** 清空 host adapter 注册表（测试隔离用） */
export function clearWidgetHostAdapters(): void {
  hostAdapters.clear()
}

// ── 声明侧：type 级动作词表 ─────────────────────────────────

/**
 * 声明 widget 类型可发的动作（声明即注册：词表进 WidgetRegistry 元数据）
 *
 * 内置域动作词表随 widget 迁移按需收敛，不预发明——每个动作以此声明为准。
 *
 * @throws type 未注册时抛错（词表必须落在已注册组件上，不允许悬空声明）
 */
export function declareWidgetActions(type: string, actions: Record<string, WidgetActionMeta>): void {
  const entry = widgetRegistry.getEntry(type)
  if (!entry) {
    throw new Error(`declareWidgetActions: widget 类型 "${type}" 未注册，动作词表须落在已注册组件上`)
  }
  entry.metadata.actions = { ...(entry.metadata.actions ?? {}), ...actions }
}

// ── 声明期 actionUri 白名单校验（extractWidgets 调用）────────

/** routes 面条目归一（/api/v1/schema routes 面：{plugin_id: [{method, path…}]}） */
function normalizeRoutesFace(face: unknown): Array<{ method: string; path: string }> {
  if (!Array.isArray(face)) return []
  const out: Array<{ method: string; path: string }> = []
  for (const r of face) {
    if (!r || typeof r !== 'object') continue
    const { method, path } = r as { method?: unknown; path?: unknown }
    if (typeof method === 'string' && typeof path === 'string') {
      out.push({ method: method.toUpperCase(), path })
    }
  }
  return out
}

/**
 * actionUri 路径是否落在路由模板面内（段级匹配）
 *
 * `{param}` 模板段允许被任意非空实例化段填充；字面段精确相等；query 串不参与
 * 匹配（路径语义，请求时原样保留）。
 */
export function actionUriMatchesRoutePath(uriPath: string, templatePath: string): boolean {
  const uriSegs = uriPath.split('?')[0].split('/').filter(Boolean)
  const tplSegs = templatePath.split('?')[0].split('/').filter(Boolean)
  if (uriSegs.length !== tplSegs.length) return false
  return tplSegs.every((seg, i) => {
    if (seg.startsWith('{') && seg.endsWith('}') && seg.length > 2) return true
    return seg === uriSegs[i]
  })
}

/**
 * 解析 ui_schema.widgets[].actions 原始声明并做白名单校验（声明期注册校验）
 *
 * 越界处置粒度 = action 项：越界/形态非法/与 host adapter 同名冲突的项拒绝
 * 注册 + console.error（fail-closed：可见的失败好过静默的放行），合法项与
 * widget 数据面、其余合法动作不受牵连。全部项被拒或无声明时返回 undefined
 * （既有声明零影响）。
 *
 * @param raw - ui_schema.widgets[].actions 原始值
 * @param pluginId - 来源插件（routes 面按插件划界）
 * @param widgetId - widget 实例标识（错误日志归因）
 * @param routesFace - /api/v1/schema routes 面中该插件的端点集合
 */
export function extractValidatedWidgetActions(
  raw: unknown,
  pluginId: string,
  widgetId: string,
  routesFace: unknown,
): Record<string, WidgetActionUriDecl> | undefined {
  if (raw == null) return undefined
  if (typeof raw !== 'object' || Array.isArray(raw)) {
    console.error(
      `[WidgetActionBus] ui_schema.actions 形态非法（须为 {动作名: {uri, method?}}），widget ${pluginId}/${widgetId} 动作全部忽略`,
    )
    return undefined
  }
  const face = normalizeRoutesFace(routesFace)
  const out: Record<string, WidgetActionUriDecl> = {}
  for (const [name, value] of Object.entries(raw)) {
    const where = `widget ${pluginId}/${widgetId} 动作 "${name}"`
    const v = value as { uri?: unknown; method?: unknown; label?: unknown; description?: unknown } | null
    if (!v || typeof v !== 'object' || typeof v.uri !== 'string' || v.uri === '') {
      console.error(`[WidgetActionBus] ${where} 形态非法（须为 {uri, method?}），拒绝注册`)
      continue
    }
    if (!v.uri.startsWith('/')) {
      console.error(`[WidgetActionBus] ${where} uri 须为绝对路径（以 / 开头），拒绝注册：${v.uri}`)
      continue
    }
    const method = typeof v.method === 'string' && v.method !== '' ? v.method.toUpperCase() : 'POST'
    // 同名冲突注册期报错（不静默遮蔽）：host adapter 优先的解析顺序下，与内置域
    // 动作同名的 actionUri 声明永远不可达——显式拒绝，让插件作者改名
    if (hasWidgetHostAdapter(name)) {
      console.error(`[WidgetActionBus] ${where} 与内置域 host adapter 同名冲突（adapter 优先将被遮蔽），拒绝注册`)
      continue
    }
    // 白名单 fail-closed：method+path 均须落在该插件自己的 routes 面内
    const matched = face.some(
      (r) => r.method === method && actionUriMatchesRoutePath(v.uri as string, r.path),
    )
    if (!matched) {
      console.error(
        `[WidgetActionBus] actionUri 越界拒绝注册（fail-closed）：${where} ${method} ${v.uri} 不在插件 ${pluginId} 自己的 routes 面内`,
      )
      continue
    }
    out[name] = {
      uri: v.uri,
      method,
      ...(typeof v.label === 'string' ? { label: v.label } : {}),
      ...(typeof v.description === 'string' ? { description: v.description } : {}),
    }
  }
  return Object.keys(out).length > 0 ? out : undefined
}

// ── 分发链（DeclaredWidgetLayer 注入 WidgetActionContext）────

/**
 * 解析并执行一次 widget 动作
 *
 * 顺序：① 内置域 host adapter（要求动作名在当前 type 的 declareWidgetActions
 * 词表内——组件只能发声明过的动作）→ ② 声明 actionUri（注册期白名单校验后
 * 仅存合法项；apiClient 直调，Authorization 照走，/ext 鉴权按 manifest 声明
 * default-deny）→ ③ 均无 = 未声明动作，显式 reject。
 */
export async function resolveWidgetAction(
  actionName: string,
  declaration: WidgetDeclaration | undefined,
  payload?: unknown,
): Promise<unknown> {
  const adapter = hostAdapters.get(actionName)
  if (adapter) {
    const vocabulary = declaration
      ? widgetRegistry.getEntry(declaration.type)?.metadata.actions
      : undefined
    if (!vocabulary || !(actionName in vocabulary)) {
      throw new Error(
        `widget 动作 "${actionName}" 不在类型 "${declaration?.type ?? '(未注册)'}" 的动作词表内（declareWidgetActions 未声明）`,
      )
    }
    return adapter(payload, declaration)
  }
  const action = declaration?.actions?.[actionName]
  if (action) {
    const resp = await apiClient.request({ url: action.uri, method: action.method, data: payload })
    // 刷新复用既有联动语义（G3 watch）：watch `{widgetId}:{actionName}` 的声明组件重挂载重拉
    emitFormEvent(`${declaration.id}:${actionName}`, resp.data)
    return resp.data
  }
  throw new Error(
    `widget 动作 "${actionName}" 未声明（host adapter 无绑定、ui_schema actions 无此动作名）`,
  )
}

// ── 组件侧 hook ─────────────────────────────────────────────

/**
 * widget 内发动作意图：返回绑定动作名的触发器
 *
 * 触发器成功 resolve 动作结果、失败 reject（要么处理要么传播）；调用未声明
 * 动作名或组件渲染在 DeclaredWidgetLayer 之外（无分发宿主）均为显式错误。
 */
export function useWidgetAction(actionName: string): (payload?: unknown) => Promise<unknown> {
  const ctx = useContext(WidgetActionContext)
  return useCallback(
    (payload?: unknown) => {
      if (!ctx) {
        return Promise.reject(
          new Error(`widget 动作 "${actionName}" 无分发宿主：组件未渲染在 DeclaredWidgetLayer 内`),
        )
      }
      return ctx.dispatch(actionName, payload)
    },
    [ctx, actionName],
  )
}
