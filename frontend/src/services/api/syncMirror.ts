/**
 * 多端同步只读快照数据面（T3 前置：只读快照 UI）
 *
 * 端点（均为已交付只读端点）：
 * - GET /ext/sync_agent/mirror/records?table=sessions|messages|tasks —— sync_agent
 *   镜像面（他端 DB 逻辑记录，结构性只读，唯一写入者 = relay 属主执法后的拉取路径）
 * - GET /ext/sync_agent/relay/devices —— 设备注册表（R30 数据面：云端权威透传，
 *   在线判定 = 心跳窗口，地盘可达性数据源；降级响应映射为拒绝 = 查询层错误态）
 * - GET /ext/sync_agent/status —— 本机配对状态（本机地盘身份）
 *
 * 纯函数助手承载呈现口径：归属过滤（owner_device）、会话呈现序（view.seq 单调）、
 * 快照时刻（记录集最大 HLC 物理毫秒，同步元数据，不取本地钟）。
 */

import apiClient from '@/services/api/client'
import { extUrl } from '@/services/api/extRoute'

/** 镜像记录表名（sync_agent capture 注册面：sessions/messages/tasks） */
export type MirrorTable = 'sessions' | 'messages' | 'tasks'

/** HLC 三元组（混合逻辑时钟：物理毫秒 + 逻辑计 + 设备） */
export interface MirrorHlc {
  physical_ms: number
  logical: number
  device_id: string
}

/** 呈现维度（消息按 pipeline 分组、seq 定序；改写按此收敛单行） */
export interface MirrorRecordView {
  group: string
  seq: number
}

/** mirror_records 行（/ext/sync_agent/mirror/records 契约） */
export interface MirrorRecord {
  asset_key: string
  table: string
  pk: string
  row: Record<string, unknown>
  owner_device: string
  deleted: boolean
  updated_at: string
  view: MirrorRecordView | null
  hlc: MirrorHlc
}

/** relay 设备注册表条目（/ext/sync_agent/relay/devices 云端权威透传） */
export interface RelayDevice {
  device_id: string
  tier: string
  app_version: string
  online: boolean
  last_heartbeat_at: string
  /** 设备自报的内核局域网地址（尽力而为；空 = 地址未知，卡上无连接入口） */
  address?: string
  /** 设备自报储存状态（数据根所在盘；null = 未上报） */
  storage?: { free_bytes: number; total_bytes: number } | null
  /** 配对中枢自身条目（可达性以注册表拉取成功为准，心跳标记对中枢冗余） */
  is_hub?: boolean
}

/** 云端权威注册表透传响应（/ext/sync_agent/relay/devices 契约） */
export interface RelayDevicesResponse {
  devices: RelayDevice[]
  /** 数据源：cloud=云端权威；none=未配对（无云端注册表可透传） */
  source: 'cloud' | 'none'
  /** 降级标记：true = 未配对/云端不可达（devices 必空，禁当空注册表消费） */
  degraded: boolean
  error?: string
  online_window_secs?: number | null
  fetched_at?: string
}

/** 本机同步状态（/ext/sync_agent/status） */
export interface SyncAgentStatus {
  loaded: boolean
  paired?: boolean
  device_id?: string
  tier?: string
  /** 中枢 relay 地址（已配对时有值；origin 即中枢内核地址，设备页直连数据源） */
  relay_url?: string
  /** 本机储存状态（数据根所在盘；null = 未上报） */
  storage?: { free_bytes: number; total_bytes: number } | null
}

/** 本机配对状态 */
export async function fetchSyncAgentStatus(): Promise<SyncAgentStatus> {
  const res = await apiClient.get<SyncAgentStatus>(extUrl('sync_agent', '/status'))
  return res.data
}

/** 单设备实测探活结果（POST /ext/sync_agent/probe 契约；ok=2xx 即通） */
export interface DeviceProbeResult {
  device_id: string
  /** 注册表地址（null = 注册表在案但无地址，不可探） */
  address: string | null
  ok: boolean
  /** HTTP 状态码（网络失败/超时为 0） */
  status: number
  latency_ms: number
  error?: string
}

/**
 * 注册表设备实测探活（地址唯一来源 = 云端权威注册表；入参 device_ids，
 * 禁任意 URL——SSRF 硬界在后端执法，前端只传 id）。
 */
export async function probeDevices(deviceIds: string[]): Promise<DeviceProbeResult[]> {
  const res = await apiClient.post<{ results: DeviceProbeResult[] }>(
    extUrl('sync_agent', '/probe'),
    { device_ids: deviceIds },
  )
  return res.data.results
}

/**
 * 设备注册表（地盘可达性：online = 心跳窗口内；R30 换源：云端权威透传面）
 *
 * 降级响应（未配对/云端不可达）映射为拒绝——查询层呈错误态（「设备注册表
 * 暂不可用」），禁把降级空数组当空注册表成功消费（会渲染「尚无其他设备」
 * 误导文案）。
 */
export async function fetchRelayDevices(): Promise<RelayDevicesResponse> {
  const res = await apiClient.get<RelayDevicesResponse>(
    extUrl('sync_agent', '/relay/devices'),
  )
  if (res.data.degraded) {
    throw new Error(res.data.error || '云端注册表暂不可达')
  }
  return res.data
}

/** 只读镜像记录（按表过滤；端点无任何写路由） */
export async function fetchMirrorRecords(table: MirrorTable): Promise<{ records: MirrorRecord[] }> {
  const res = await apiClient.get<{ records: MirrorRecord[] }>(
    extUrl('sync_agent', '/mirror/records'),
    { params: { table } },
  )
  return res.data
}

/** 指定属主地盘的未删除镜像会话（墓碑不呈现） */
export function sessionsOfTerritory(records: MirrorRecord[], deviceId: string): MirrorRecord[] {
  return records.filter(
    (r) => r.table === 'sessions' && r.owner_device === deviceId && !r.deleted,
  )
}

/** 指定会话（view.group = 会话 id）的消息，按 view.seq 升序（呈现序不变量） */
export function messagesOfSession(records: MirrorRecord[], threadId: string): MirrorRecord[] {
  return records
    .filter((r) => r.table === 'messages' && !r.deleted && r.view?.group === threadId)
    .sort((a, b) => (a.view?.seq ?? 0) - (b.view?.seq ?? 0))
}

/** 快照时刻 = 记录集最大 HLC 物理毫秒（同步元数据；空集 null） */
export function snapshotTimeOf(records: MirrorRecord[]): number | null {
  let max: number | null = null
  for (const r of records) {
    if (max === null || r.hlc.physical_ms > max) max = r.hlc.physical_ms
  }
  return max
}

/** 快照时刻本地化（分钟粒度 YYYY-MM-DD HH:mm） */
export function formatSnapshotTime(ms: number): string {
  const d = new Date(ms)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

/** 字节数 humanize（一位小数，GB 口径；设备卡储存状态呈现用） */
export function formatGb(bytes: number): string {
  return `${(bytes / 1024 ** 3).toFixed(1)}G`
}

/** 储存状态行文案（「剩余 12.3G / 64.0G」；null = 未上报不呈现） */
export function formatStorageLine(storage: {
  free_bytes: number
  total_bytes: number
}): string {
  return `剩余 ${formatGb(storage.free_bytes)} / ${formatGb(storage.total_bytes)}`
}
