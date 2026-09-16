/** 文件树节点展示格式化（优先级标签/时间戳），自 FileTreeWidget 拆出。 */

/** 优先级标签映射 */
export const PRIORITY_LABELS: Record<string, { label: string; color: string }> = {
  critical: { label: '紧急', color: 'text-status-error' },
  high: { label: '高', color: 'text-status-warning' },
  normal: { label: '普通', color: 'text-muted-foreground' },
  low: { label: '低', color: 'text-muted-foreground/60' },
}

/** 格式化时间戳为可读字符串 */
export function formatTime(value: string | null | undefined): string | null {
  if (!value) return null
  try {
    const d = new Date(value)
    if (isNaN(d.getTime())) return null
    const mm = String(d.getMonth() + 1).padStart(2, '0')
    const dd = String(d.getDate()).padStart(2, '0')
    const hh = String(d.getHours()).padStart(2, '0')
    const mi = String(d.getMinutes()).padStart(2, '0')
    return `${mm}-${dd} ${hh}:${mi}`
  } catch {
    return null
  }
}
