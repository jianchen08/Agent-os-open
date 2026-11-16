/**
 * 提示音目录服务——前端与 notification_sounds 插件的接线（纯服务，无 React）。
 *
 * 数据真源 = notification_sounds 插件的 sounds/*.json（内置与用户/agent 自定义
 * 同权，落文件即上目录），经 GET /ext/notification_sounds/sounds 全量下发。
 * 前端不做任何音效硬编码：本模块只透传目录 + 承载合成参数的类型契约。
 */
import apiClient from '@/services/api/client'
import { NOTIFICATION_SOUNDS_ENDPOINTS } from '@/services/api/endpoints.generated'

/** 泛音簇成分：频率比 × 增益，decayRatio 相对基频衰减时长的比例 */
export interface SoundSynthPartial {
  ratio: number
  gain: number
  decayRatio: number
}

/** 软混响抽头：延迟秒数 / 反馈系数 / 环路低通截止 Hz */
export interface SoundSynthReverbTap {
  delay: number
  feedback: number
  lowpass: number
}

/** 单音：可选滑音终点（指数滑音）与颤音（相对基频的深度比例） */
export interface SoundSynthNote {
  freq: number
  start: number
  peak: number
  decay: number
  glideTo?: number
  glideTime?: number
  vibratoHz?: number
  vibratoDepth?: number
}

/** Web Audio 合成参数（notification_sounds 预设 params 字段契约） */
export interface SoundSynthParams {
  partials: SoundSynthPartial[]
  notes: SoundSynthNote[]
  /** 起音时长（秒），缺省 0.006 */
  attack?: number
  /** 湿声比例 0~1，缺省 0（干声） */
  wet?: number
  /** 反馈延迟混响抽头，wet>0 时应提供 */
  taps?: SoundSynthReverbTap[]
}

/** 提示音预设（sounds/*.json 文件契约的透传） */
export interface NotificationSoundPreset {
  id: string
  label: string
  engine?: string
  default?: boolean
  params: SoundSynthParams
}

export interface NotificationSoundCatalog {
  sounds: NotificationSoundPreset[]
  defaultId: string
}

interface CatalogResponse {
  sounds?: NotificationSoundPreset[]
  default_id?: string
}

/** 拉取提示音目录（插件读盘即热；失败抛错由消费方定降级） */
export async function fetchNotificationSoundCatalog(): Promise<NotificationSoundCatalog> {
  const resp = await apiClient.get<CatalogResponse>(NOTIFICATION_SOUNDS_ENDPOINTS.sounds_catalog)
  return {
    sounds: Array.isArray(resp.data?.sounds) ? resp.data.sounds : [],
    defaultId: typeof resp.data?.default_id === 'string' ? resp.data.default_id : '',
  }
}
