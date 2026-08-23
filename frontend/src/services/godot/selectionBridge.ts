/**
 * Godot 选中引用桥——前端与 pipeline_godot_context 插件的接线（纯服务，无 React）。
 *
 * 事件流向（全程由 Godot 侧发起推送，前端零轮询）：
 *   Godot EditorSelection.selection_changed
 *     → POST pipeline_godot_context selection（宿主插件推送）
 *     → 插件 emit godot_selection_changed（按订阅 thread_id 单播）
 *     → 本服务经 globalWS 订阅更新状态 → 聊天输入框引用卡片实时镜像
 *
 * 初始化（initGodotSelection）：订阅 thread + 拉取当前快照 + 挂 WS 事件监听（幂等，仅首次挂）。
 */
import apiClient from '@/services/api/client'
import { PIPELINE_GODOT_CONTEXT_ENDPOINTS } from '@/services/api/endpoints.generated'
import { globalWS } from '@/services/websocket/GlobalWebSocket'
import { WS_SERVER_EVENTS } from '@/constants/websocket'

export interface GodotSelectionItem {
  name: string
  type: string
  path: string
  /** texture=贴图缩略图 / viewport=编辑器视口截图 / 空=无预览 */
  preview_kind?: string
}

export interface GodotSelectionScene {
  name?: string
  path?: string
  root?: string
}

export interface GodotSelectionState {
  connected: boolean
  items: GodotSelectionItem[]
  signature: string
  scene?: GodotSelectionScene
  engine_version?: string
}

const ENDPOINTS = {
  selection: PIPELINE_GODOT_CONTEXT_ENDPOINTS.selection_push,
  subscribe: PIPELINE_GODOT_CONTEXT_ENDPOINTS.selection_subscribe,
}

/** 预览图 URL（经插件代理 Godot 9600；v=签名，选中变化时刷新缓存） */
export function godotPreviewUrl(index: number, signature: string): string {
  return `${PIPELINE_GODOT_CONTEXT_ENDPOINTS.selection_preview}?index=${index}&v=${encodeURIComponent(signature)}`
}

const EMPTY_STATE: GodotSelectionState = { connected: false, items: [], signature: '' }

let state: GodotSelectionState = EMPTY_STATE
const listeners = new Set<(s: GodotSelectionState) => void>()
let wsHooked = false
let currentThread = ''

function setState(next: GodotSelectionState): void {
  state = next
  listeners.forEach((fn) => fn(state))
}

/** 当前快照（同步读） */
export function getGodotSelection(): GodotSelectionState {
  return state
}

/** 订阅状态变化（返回取消函数） */
export function subscribeGodotSelection(fn: (s: GodotSelectionState) => void): () => void {
  listeners.add(fn)
  return () => {
    listeners.delete(fn)
  }
}

function hookWsEvents(): void {
  if (wsHooked) return
  wsHooked = true
  globalWS.subscribe(WS_SERVER_EVENTS.GODOT_SELECTION_CHANGED, (payload: unknown) => {
    const data = (payload as { data?: GodotSelectionState & { thread_id?: string } })?.data
    if (!data) return
    if (data.thread_id && currentThread && data.thread_id !== currentThread) return
    setState({
      connected: !!data.connected,
      items: Array.isArray(data.items) ? data.items : [],
      signature: data.signature ?? '',
      scene: data.scene,
      engine_version: data.engine_version,
    })
  })
}

/**
 * 初始化/切换线程：订阅该 thread 的推送并拉取当前快照。
 * 失败静默（内核未启动 / 插件未加载时保持未连接状态）。
 */
export async function initGodotSelection(threadId: string): Promise<void> {
  currentThread = threadId ?? ''
  hookWsEvents()
  try {
    await apiClient.post(ENDPOINTS.subscribe, { thread_id: threadId })
  } catch {
    // 内核未启动或插件未加载——保持未连接，事件来了自然恢复
  }
  try {
    const resp = await apiClient.get<GodotSelectionState>(ENDPOINTS.selection)
    const snap = resp.data
    if (snap && typeof snap === 'object' && 'items' in snap) {
      setState({
        connected: !!snap.connected,
        items: Array.isArray(snap.items) ? snap.items : [],
        signature: snap.signature ?? '',
        scene: snap.scene,
        engine_version: snap.engine_version,
      })
    }
  } catch {
    // 同上，静默
  }
}
