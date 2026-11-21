/**
 * uiBridge 命令执行器（冻结件 §2.4 前端执行面，P1 命令闭环）
 *
 * 订阅 WS `ui_command`（载荷在帧 .data，仓内 handler 收全帧惯例），按封闭词表
 * 映射既有 store/opener 路径执行，随后 POST `/ext/ui_bridge/command_result`
 * 回执（多端并存各端各执行各回执，插件侧取首个 applied）。未知 action 或
 * 参数形状非法回执 rejected——插件侧发令前已 fail-closed 校验，此处是直发
 * WS 的最后一道防线。highlight 高亮态经 subscribeHighlight 广播（Sidebar
 * 高亮最小增量的对接点；uiStore 不新增字段）。
 *
 * @feature FP-0.2.侧边栏UI对象化 侧边栏UI对象化P1命令闭环 | @vision V2 全能闭环 | @ci frontend-test
 */
import { WS_SERVER_EVENTS } from '@/constants/websocket'
import apiClient from '@/services/api/client'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import { globalWS } from '@/services/websocket/GlobalWebSocket'
import { openPluginPage } from '@/services/workspacePanelOpener'
import { useNotificationStore } from '@/stores/notificationStore'
import { useUIStore } from '@/stores/uiStore'

/** ui_command 帧载荷（§2.4 data = {command_id, region, action, params}） */
export interface UiCommandData {
  command_id: string
  region?: string
  action: string
  params: Record<string, unknown>
}

/** 回执体（POST /ext/ui_bridge/command_result） */
export interface CommandReceiptBody {
  command_id: string
  status: 'applied' | 'rejected'
  reason?: string
}

/** 执行器依赖注入点（缺省 = 真实外发面） */
export interface CommandExecutorDeps {
  /** 回执外发注入点（唯一 HTTP mock 点）；缺省 = apiClient POST 既有客户端 */
  postResult?: (body: CommandReceiptBody) => Promise<void>
  /** open_page 注入点；缺省 = workspacePanelOpener 既有路径（main.tsx 先例同构） */
  openPage?: (pageId: string) => unknown
}

export interface CommandExecutorHandle {
  /** 拆除：退订 ui_command，后续帧零回执零副作用 */
  stop(): void
}

/** 高亮态（highlight 命令到达 = {itemId}；duration_ms 到点消退 = null） */
export type HighlightListener = (highlight: { itemId: string } | null) => void

const DEFAULT_HIGHLIGHT_MS = 2000
const MAX_HIGHLIGHT_MS = 5000
const COMMAND_RESULT_URL = '/ext/ui_bridge/command_result'

// ── highlight 高亮态广播（模块级：Sidebar 订阅面与执行器解耦）──────────

const highlightListeners = new Set<HighlightListener>()
let highlightTimer: ReturnType<typeof setTimeout> | null = null

export function subscribeHighlight(listener: HighlightListener): () => void {
  highlightListeners.add(listener)
  return () => {
    highlightListeners.delete(listener)
  }
}

function broadcastHighlight(itemId: string, durationMs: number): void {
  if (highlightTimer !== null) {
    clearTimeout(highlightTimer)
    highlightTimer = null
  }
  for (const listener of highlightListeners) {
    listener({ itemId })
  }
  highlightTimer = setTimeout(() => {
    highlightTimer = null
    for (const listener of highlightListeners) {
      listener(null)
    }
  }, durationMs)
}

// ── 命令执行（映射既有 store/opener 路径，§2.4 逐字）───────────────────

function defaultOpenPage(pageId: string): unknown {
  const page = contributionRegistry.getPages().find((p) => p.id === pageId)
  if (!page) {
    // 插件侧按快照 items 校验过目标，此处缺声明 = 声明面在快照后失效（插件
    // 禁用/卸载）——显式通知不静默（与 workspacePanelOpener 失败口径一致）。
    useNotificationStore.getState().addNotification({
      title: '页面无法打开',
      message: `页面 ${pageId} 未声明（插件可能已禁用），无法打开`,
      priority: 'high',
      category: 'error',
      isBlocking: false,
    })
    return false
  }
  return openPluginPage(page)
}

function defaultPostResult(body: CommandReceiptBody): Promise<void> {
  return apiClient.post(COMMAND_RESULT_URL, body).then(() => undefined)
}

/** 命令 → 动作 + 回执（动作同步执行；形状非法一律 rejected 且零副作用） */
function executeCommand(
  data: UiCommandData,
  openPage: (pageId: string) => unknown,
): CommandReceiptBody {
  const params = (data.params ?? {}) as Record<string, unknown>
  switch (data.action) {
    case 'navigate': {
      const target = params['target']
      if (typeof target !== 'string' || !target) {
        return { command_id: data.command_id, status: 'rejected' }
      }
      useUIStore.getState().setSidebarActiveView(target)
      return { command_id: data.command_id, status: 'applied' }
    }
    case 'set_collapsed': {
      const collapsed = params['collapsed']
      if (typeof collapsed !== 'boolean') {
        return { command_id: data.command_id, status: 'rejected' }
      }
      useUIStore.getState().setSidebarCollapsed(collapsed)
      return { command_id: data.command_id, status: 'applied' }
    }
    case 'open_page': {
      const pageId = params['page_id']
      if (typeof pageId !== 'string' || !pageId) {
        return { command_id: data.command_id, status: 'rejected' }
      }
      openPage(pageId)
      return { command_id: data.command_id, status: 'applied' }
    }
    case 'highlight': {
      const itemId = params['item_id']
      if (typeof itemId !== 'string' || !itemId) {
        return { command_id: data.command_id, status: 'rejected' }
      }
      const raw = params['duration_ms']
      const duration =
        typeof raw === 'number' && Number.isFinite(raw)
          ? Math.min(Math.max(raw, 0), MAX_HIGHLIGHT_MS)
          : DEFAULT_HIGHLIGHT_MS
      broadcastHighlight(itemId, duration)
      return { command_id: data.command_id, status: 'applied' }
    }
    default:
      return { command_id: data.command_id, status: 'rejected' }
  }
}

/** 全帧载荷提取（仓内惯例 handler 收全帧；容忍直接传 data 的直发方） */
function extractUiCommandData(frame: unknown): UiCommandData | null {
  const candidate =
    typeof frame === 'object' && frame !== null && 'data' in frame
      ? (frame as { data: unknown }).data
      : frame
  if (typeof candidate !== 'object' || candidate === null) return null
  const data = candidate as Partial<UiCommandData>
  if (typeof data.command_id !== 'string' || !data.command_id) return null
  if (typeof data.action !== 'string') return null
  return { command_id: data.command_id, region: data.region, action: data.action, params: data.params ?? {} }
}

/**
 * 启动命令执行器：订阅 ui_command → 执行 → 回执。
 *
 * 多端并存（AC-P1-5）：每个执行器实例独立订阅同一事件，同帧各执行各回执。
 */
export function startCommandExecutor(deps?: CommandExecutorDeps): CommandExecutorHandle {
  const postResult = deps?.postResult ?? defaultPostResult
  const openPage = deps?.openPage ?? defaultOpenPage
  let stopped = false

  const handler = (frame: unknown): void => {
    if (stopped) return
    const data = extractUiCommandData(frame)
    if (data === null) return
    const receipt = executeCommand(data, openPage)
    void postResult(receipt).catch(() => {
      // HACK: 回执外发 fire-and-forget，失败不外抛——插件侧 2s 无回执如实
      // timeout，下一命令自然重试（OBS-R258-1 吞错误规则登记）
    })
  }

  globalWS.subscribe(WS_SERVER_EVENTS.UI_COMMAND, handler)

  return {
    stop() {
      stopped = true
      globalWS.unsubscribe(WS_SERVER_EVENTS.UI_COMMAND, handler)
    },
  }
}
