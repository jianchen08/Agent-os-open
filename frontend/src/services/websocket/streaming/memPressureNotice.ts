/**
 * 内存压力告警 system_notification 的专项呈现（ADR 2026-10-19 D5 执法序列
 * 第一级：告警进通知中心，内核面 commit e84b2eebb）。
 *
 * 内核 plugin_notices（kernel/crates/api/src/plugin_notices.rs）把采样循环
 * WARN 阈值判定的进入/退出沿各发一条（持续态不重复），经既有
 * `system_notification` 事件双路投递（活跃管道内联 + 全连接广播），复用插件
 * 不可用同一契约面：record_id 同前缀 `n_plugin_unavailable_`，
 * kind=mem_pressure（level=error）/ mem_pressure_cleared（level=success）。
 * 观测数字（commit/abandoned 对阈值）只进正文 content，无结构化字段。
 *
 * mem 族与插件健康面（pluginHealthNotice）同事件并行：本模块处理 mem 族，
 * 返回消费与否供 streaming/index.ts 分流——mem 命中即短路，不落插件健康面
 * （内存压力不是插件降级，登进降级横幅=语义污染）。
 */
import { toast } from '@/components/ui/sonner'

/** 进入沿 kind（commit/abandoned 超 WARN 阈值） */
export const MEM_PRESSURE_KIND = 'mem_pressure'

/** 退出沿 kind（回落 WARN 阈值下，恢复语义） */
export const MEM_PRESSURE_CLEARED_KIND = 'mem_pressure_cleared'

export type MemPressureKind = typeof MEM_PRESSURE_KIND | typeof MEM_PRESSURE_CLEARED_KIND

/** 后端 plugin_notices.record_id 前缀契约（mem 族与插件不可用族共用） */
export const MEM_NOTICE_RECORD_PREFIX = 'n_plugin_unavailable_'

/** 从 record_id 解析 kind 段（n_plugin_unavailable_{kind}_{ts}）；无 kind 段回退 unknown */
function recordIdKind(recordId: string): string {
  const rest = recordId.slice(MEM_NOTICE_RECORD_PREFIX.length)
  const kind = rest.slice(0, rest.lastIndexOf('_'))
  return kind !== '' ? kind : 'unknown'
}

/**
 * kind 判定：notificationType 权威（=内核 kind），缺省从 record_id kind 段
 * 回退；非 mem 族（含未知 kind / 缺 record_id）→ null。
 */
export function resolveMemPressureKind(
  notificationType: unknown,
  recordId: unknown,
): MemPressureKind | null {
  let kind: string
  if (typeof notificationType === 'string' && notificationType !== '') {
    kind = notificationType
  } else if (typeof recordId === 'string' && recordId.startsWith(MEM_NOTICE_RECORD_PREFIX)) {
    kind = recordIdKind(recordId)
  } else {
    return null
  }
  if (kind === MEM_PRESSURE_KIND || kind === MEM_PRESSURE_CLEARED_KIND) {
    return kind
  }
  return null
}

/** 正文观测数字（MB / 页），解析失败缺数 undefined */
export interface MemObservation {
  commitMb?: number
  abandonedPages?: number
  thresholdCommitMb?: number
  thresholdAbandonedPages?: number
}

/**
 * 从正文解析观测数字（内核正文是唯一数字来源；格式漂移 → 缺数 → 呈现 '—'）。
 * 内核正文形态（main.rs 采样循环）：进入沿
 * 「进程 commit {N}MB（WARN 阈值 {N}MB）、abandoned {N} 页（阈值 {N}）超限…」；
 * 退出沿「进程 commit {N}MB、abandoned {N} 页已回落 WARN 阈值下…」。
 */
export function parseMemObservation(content: string): MemObservation {
  const capture = (re: RegExp): number | undefined => {
    const m = re.exec(content)
    if (!m) return undefined
    const v = Number(m[1])
    return Number.isFinite(v) ? v : undefined
  }
  return {
    commitMb: capture(/commit (\d+)MB/),
    thresholdCommitMb: capture(/WARN 阈值 (\d+)MB/),
    abandonedPages: capture(/abandoned (\d+) 页/),
    thresholdAbandonedPages: capture(/页（阈值 (\d+)）/),
  }
}

/** MB → GB 文案（1 位小数去尾零：8192→'8GB'，25702→'25.1GB'） */
export function formatGb(mb: number): string {
  const gb = (mb / 1024).toFixed(1)
  return `${gb.endsWith('.0') ? gb.slice(0, -2) : gb}GB`
}

/** 页 → 万 文案（1 位小数去尾零：50000→'5万'，460000→'46万'） */
export function formatWan(pages: number): string {
  const wan = (pages / 10000).toFixed(1)
  return `${wan.endsWith('.0') ? wan.slice(0, -2) : wan}万`
}

/** toast 呈现载荷（severity=sonner 方法名） */
export interface MemPressureMessage {
  severity: 'error' | 'success'
  title: string
  description: string
}

/**
 * kind → 呈现文案：进入沿 error 醒目（title 含「内存高压」+ 观测数字对，
 * 缺数 '—'）；退出沿 success 轻呈现（title「内存压力解除」，内核正文承载
 * 回落数字）。description 统一透传内核正文（嫌疑/处置说明不丢）。
 */
export function buildMemPressureMessage(
  kind: MemPressureKind,
  content: string,
): MemPressureMessage {
  if (kind === MEM_PRESSURE_CLEARED_KIND) {
    return { severity: 'success', title: '内存压力解除', description: content }
  }
  const obs = parseMemObservation(content)
  const fmt = (v: number | undefined, f: (n: number) => string): string =>
    v === undefined ? '—' : f(v)
  const title =
    `内存高压：commit ${fmt(obs.commitMb, formatGb)} / ` +
    `废弃页 ${fmt(obs.abandonedPages, formatWan)}` +
    `（阈值 ${fmt(obs.thresholdCommitMb, formatGb)} / ` +
    `${fmt(obs.thresholdAbandonedPages, formatWan)}）`
  return { severity: 'error', title, description: content }
}

/** 解析后的 mem 族事件（只承载本模块消费的字段） */
interface ParsedMemEvent {
  recordId: string
  kind: MemPressureKind
  content: string
}

/** 事件解析（兼容 {data:{...}} 包裹与扁平两种投递形态，同
 * handleSystemNotification 取值口径）；非 mem 族/缺 record_id/缺正文 → null。 */
function parseMemEvent(eventData: unknown): ParsedMemEvent | null {
  if (!eventData || typeof eventData !== 'object') return null
  const rec = eventData as { data?: unknown }
  const data = (rec.data && typeof rec.data === 'object' ? rec.data : eventData) as Record<string, unknown>
  const recordId = data.record_id
  if (typeof recordId !== 'string' || recordId === '') return null
  const kind = resolveMemPressureKind(data.notificationType, recordId)
  if (!kind) return null
  const content = typeof data.content === 'string' ? data.content : ''
  if (content === '') return null
  return { recordId, kind, content }
}

/**
 * mem 族 system_notification 入口：专项 toast（进入沿 error / 退出沿
 * success，id=record_id 幂等——WS 重连重放同 id 重入不刷屏）。返回是否
 * 消费：true 时调用方跳过插件健康面（record_id 同前缀但语义不同）。
 */
export function handleMemPressureNotice(eventData: unknown): boolean {
  const event = parseMemEvent(eventData)
  if (!event) return false
  const msg = buildMemPressureMessage(event.kind, event.content)
  toast[msg.severity](msg.title, { description: msg.description, id: event.recordId })
  return true
}
