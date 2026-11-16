/**
 * 插件不可用 system_notification 的全局健康面处理（卡3 前端工单）。
 *
 * 后端 kernel plugin_notices（kernel/crates/api/src/plugin_notices.rs）把三路
 * 判定源（spawn_breaker / cohost_member_offline / verify_failed）经既有
 * `system_notification` 事件直发活跃管道，载荷对齐 handleSystemNotification
 * 契约且 record_id 恒带前缀 `n_plugin_unavailable_`——前缀即健康类识别标记。
 *
 * 本模块与 lifecycleHandlers.handleSystemNotification（会话气泡路径，经管道
 * 相关性门控）并行：健康面是全局的（降级影响所有会话的工具面），必须绕过
 * 门控点亮 toast + 降级横幅。刻意不进 NotificationCenter（自动弹全屏抽屉与
 * 常驻横幅语义冲突，见 pluginHealthStore 头注）。
 */
import { toast } from '@/components/ui/sonner'
import { usePluginHealthStore } from '@/stores/pluginHealthStore'

/** 后端 record_id() 契约前缀（kind+时间戳） */
export const PLUGIN_NOTICE_RECORD_PREFIX = 'n_plugin_unavailable_'

/** 解析后的健康类通知（只承载本模块消费的字段） */
interface ParsedNotice {
  recordId: string
  content: string
  level?: string
  notificationType?: string
}

/**
 * 从事件解析健康类通知（兼容 {data:{...}} 包裹与扁平两种投递形态，同
 * handleSystemNotification 取值口径）；非健康类/缺 record_id/缺正文 → null。
 */
function parseNotice(eventData: unknown): ParsedNotice | null {
  if (!eventData || typeof eventData !== 'object') return null
  const rec = eventData as { data?: unknown }
  const data = (rec.data && typeof rec.data === 'object' ? rec.data : eventData) as Record<string, unknown>
  const recordId = data.record_id
  if (typeof recordId !== 'string' || !recordId.startsWith(PLUGIN_NOTICE_RECORD_PREFIX)) {
    return null
  }
  const content = typeof data.content === 'string' ? data.content : ''
  if (content === '') return null
  const level = typeof data.level === 'string' ? data.level : undefined
  const notificationType =
    typeof data.notificationType === 'string' && data.notificationType !== ''
      ? data.notificationType
      : undefined
  return { recordId, content, level, notificationType }
}

/** 健康类识别：record_id 带 n_plugin_unavailable_ 前缀（缺 record_id 不命中） */
export function isPluginUnavailableNotice(eventData: unknown): boolean {
  return parseNotice(eventData) !== null
}

/** level → toast 严重度（后端现役恒 error；warn 族归 warning，其余归 info） */
export function toastSeverityForLevel(level: unknown): 'error' | 'warning' | 'info' {
  if (level === 'error' || level === 'critical') return 'error'
  if (level === 'warning' || level === 'warn') return 'warning'
  return 'info'
}

/** 正文主语：「当前不可用」前的组名/成员段；无标记（格式漂移）全文兜底 */
export function extractNoticeSubject(content: string): string {
  const idx = content.indexOf('当前不可用')
  return idx > 0 ? content.slice(0, idx) : content
}

/** kind 从 record_id 解析（n_plugin_unavailable_{kind}_{ts}）；无 kind 段回退 unknown */
function extractKind(recordId: string): string {
  const rest = recordId.slice(PLUGIN_NOTICE_RECORD_PREFIX.length)
  const kind = rest.slice(0, rest.lastIndexOf('_'))
  return kind !== '' ? kind : 'unknown'
}

/**
 * 健康类 system_notification 入口：toast（按 level 定严重度，id=record_id
 * 防重放重复弹）+ 降级横幅登记（同键 upsert）。非健康类事件零触碰。
 */
export function handlePluginUnavailableNotice(eventData: unknown): void {
  const notice = parseNotice(eventData)
  if (!notice) return

  const subject = extractNoticeSubject(notice.content)
  const severity = toastSeverityForLevel(notice.level)
  toast[severity](subject, { description: notice.content, id: notice.recordId })

  // 判定种类：载荷 notificationType 是权威（=kind）；缺省从 record_id 解析回退
  const kind = notice.notificationType ?? extractKind(notice.recordId)

  usePluginHealthStore.getState().reportUnavailable({
    key: `${kind}|${subject}`,
    kind,
    subject,
    content: notice.content,
    level: notice.level ?? 'info',
  })
}
