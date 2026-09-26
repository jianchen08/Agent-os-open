/**
 * 宿主选中引用桥——前端与 pipeline_host_context 插件的接线（纯服务，无 React）。
 *
 * 事件流向（全程由宿主侧发起推送，前端零轮询）：
 *   宿主编辑器选中变化（如 Godot EditorSelection.selection_changed）
 *     → POST pipeline_host_context selection（宿主插件推送）
 *     → 插件 emit host_selection_changed（按订阅 thread_id 单播）
 *     → 本服务经 globalWS 订阅更新状态 → 聊天输入框引用卡片实时镜像
 *
 * 连接者身份（source/display_name）随快照下发，本桥与消费方不烧死任何宿主名。
 *
 * 初始化（initHostSelection）：订阅 thread + 拉取当前快照 + 挂 WS 事件监听（幂等，仅首次挂）。
 */
import { WS_LOCAL_EVENTS, WS_SERVER_EVENTS } from '@/constants/websocket'
import apiClient from '@/services/api/client'
import { PIPELINE_HOST_CONTEXT_ENDPOINTS } from '@/services/api/endpoints.generated'
import { ErrorSeverity, ErrorType, reportError } from '@/services/errorReporting'
import { globalWS } from '@/services/websocket/GlobalWebSocket'

export interface HostSelectionItem {
  name: string
  type: string
  path: string
  /** 节点全局坐标（2D/3D），文件选中为空 */
  position?: string
  /** texture=贴图缩略图 / viewport=编辑器视口截图 / 空=无预览 */
  preview_kind?: string
}

export interface HostSelectionScene {
  name?: string
  path?: string
  root?: string
}

export interface HostSelectionState {
  connected: boolean
  items: HostSelectionItem[]
  signature: string
  scene?: HostSelectionScene
  engine_version?: string
  /** 连接者标识（引用块 source，来自插件配置） */
  source?: string
  /** 连接者显示名（镜像行标签，来自插件配置） */
  display_name?: string
}

const ENDPOINTS = {
  selection: PIPELINE_HOST_CONTEXT_ENDPOINTS.selection_push,
  subscribe: PIPELINE_HOST_CONTEXT_ENDPOINTS.selection_subscribe,
  clear: PIPELINE_HOST_CONTEXT_ENDPOINTS.selection_clear,
}

/** 预览图 URL（经插件代理宿主 preview_endpoint；v=签名，选中变化时刷新缓存） */
export function hostPreviewUrl(index: number, signature: string): string {
  return `${PIPELINE_HOST_CONTEXT_ENDPOINTS.selection_preview}?index=${index}&v=${encodeURIComponent(signature)}`
}

const EMPTY_STATE: HostSelectionState = { connected: false, items: [], signature: '' }

let state: HostSelectionState = EMPTY_STATE
const listeners = new Set<(s: HostSelectionState) => void>()
let wsHooked = false
let currentThread = ''

function setState(next: HostSelectionState): void {
  state = next
  listeners.forEach((fn) => fn(state))
}

/** 当前快照（同步读） */
export function getHostSelection(): HostSelectionState {
  return state
}

/** 订阅状态变化（返回取消函数） */
export function subscribeHostSelection(fn: (s: HostSelectionState) => void): () => void {
  listeners.add(fn)
  return () => {
    listeners.delete(fn)
  }
}

function hookWsEvents(): void {
  if (wsHooked) return
  wsHooked = true
  // sidecar 重载/重连会清空插件订阅表——重连后重新订阅当前线程并拉快照，
  // 否则实时事件静默失效（必须手动刷新页面才恢复）。
  globalWS.subscribe(WS_LOCAL_EVENTS.RECONNECTED, () => {
    if (currentThread) void initHostSelection(currentThread)
  })
  // 低频重申订阅：sidecar 重载（插件热更新等）会清空其内存订阅表且无前端可感知
  // 信号——30s 重发一次 subscribe（幂等微请求），页面自愈无需手动刷新。
  // 非核心失败不阻断自愈循环，但禁静默：经统一错误上报链给降级提示；
  // 连续失败期间只报一次（episode 去重，恢复成功重置），避免 30s 刷屏。
  let resubscribeFailing = false
  window.setInterval(() => {
    if (!currentThread) return
    apiClient
      .post(ENDPOINTS.subscribe, { thread_id: currentThread })
      .then(() => {
        resubscribeFailing = false
      })
      .catch(() => {
        if (resubscribeFailing) return
        resubscribeFailing = true
        reportError('宿主选中引用订阅失败，实时镜像可能停更（每 30s 自动重试恢复）', {
          type: ErrorType.NETWORK,
          severity: ErrorSeverity.WARNING,
          component: 'hostBridge',
          action: 'selection_subscribe_retry',
          source: 'frontend',
        })
      })
  }, 30_000)
  globalWS.subscribe(WS_SERVER_EVENTS.HOST_SELECTION_CHANGED, (payload: unknown) => {
    const data = (payload as { data?: HostSelectionState & { thread_id?: string } })?.data
    if (!data) return
    if (data.thread_id && currentThread && data.thread_id !== currentThread) return
    setState({
      connected: !!data.connected,
      items: Array.isArray(data.items) ? data.items : [],
      signature: data.signature ?? '',
      scene: data.scene,
      engine_version: data.engine_version,
      source: data.source,
      display_name: data.display_name,
    })
  })
}

/**
 * 清除当前引用（点击清理）：插件清空快照并抑制同签名心跳（改选/重新点选恢复）。
 * 失败不本地假清——卡片与插件真实状态保持一致（下次消息仍会注入，诚实可见）。
 */
export async function clearHostSelection(): Promise<boolean> {
  try {
    await apiClient.delete(ENDPOINTS.clear)
  } catch {
    return false
  }
  // 服务端已清并广播空 items；本地同步置空（WS 事件到达时同值幂等）
  setState({ ...state, items: [], signature: '' })
  return true
}

/**
 * 初始化/切换线程：订阅该 thread 的推送并拉取当前快照。
 * 失败静默（内核未启动 / 插件未加载时保持未连接状态）。
 */
export async function initHostSelection(threadId: string): Promise<void> {
  currentThread = threadId ?? ''
  hookWsEvents()
  try {
    await apiClient.post(ENDPOINTS.subscribe, { thread_id: threadId })
  } catch {
    // 内核未启动或插件未加载——保持未连接，事件来了自然恢复
  }
  try {
    const resp = await apiClient.get<HostSelectionState>(ENDPOINTS.selection)
    const snap = resp.data
    if (snap && typeof snap === 'object' && 'items' in snap) {
      setState({
        connected: !!snap.connected,
        items: Array.isArray(snap.items) ? snap.items : [],
        signature: snap.signature ?? '',
        scene: snap.scene,
        engine_version: snap.engine_version,
        source: snap.source,
        display_name: snap.display_name,
      })
    }
  } catch {
    // 同上，静默
  }
}
