/**
 * 格式化工具函数
 */

/**
 * 解析时间戳字符串为 Date 对象
 * 后端返回的时间是 UTC 时间，需要正确转换为本地时间
 * @param timestamp - ISO时间戳字符串（可能带或不带时区标识）
 * @returns Date 对象
 */
export function parseUTCTimestamp(timestamp: string): Date {
  if (!timestamp) {
    return new Date()
  }
  
  if (timestamp.endsWith('Z') || /[+-]\d{2}:\d{2}$/.test(timestamp)) {
    return new Date(timestamp)
  }
  
  return new Date(timestamp + 'Z')
}

/**
 * 格式化相对时间（如"刚刚"、"5分钟前"）
 * @param date - 日期对象
 * @returns 相对时间字符串
 */
export function formatRelativeTime(date: Date): string {
  const now = new Date()
  const diffMs = now.getTime() - date.getTime()
  const diffSeconds = Math.floor(diffMs / 1000)
  const diffMinutes = Math.floor(diffSeconds / 60)
  const diffHours = Math.floor(diffMinutes / 60)
  const diffDays = Math.floor(diffHours / 24)

  if (diffSeconds < 60) {
    return '刚刚'
  } else if (diffMinutes < 60) {
    return `${diffMinutes}分钟前`
  } else if (diffHours < 24) {
    return `${diffHours}小时前`
  } else if (diffDays < 7) {
    return `${diffDays}天前`
  } else {
    return formatDateString(date)
  }
}

/**
 * 格式化日期字符串（内部使用，避免循环调用）
 * @param date - 日期对象
 * @returns 格式化后的日期字符串
 */
function formatDateString(date: Date): string {
  return date.toLocaleDateString('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  })
}

/**
 * 格式化日期时间
 * @param dateString - ISO日期字符串
 * @param format - 格式类型
 * @returns 格式化后的日期字符串
 */
export function formatDate(
  dateString: string,
  format: 'full' | 'date' | 'time' | 'relative' = 'full'
): string {
  const date = parseUTCTimestamp(dateString)

  if (isNaN(date.getTime())) {
    return '无效日期'
  }

  switch (format) {
    case 'full':
      return date.toLocaleString('zh-CN', {
        year: 'numeric',
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
      })

    case 'date':
      return formatDateString(date)

    case 'time':
      return date.toLocaleTimeString('zh-CN', {
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
      })

    case 'relative':
      return formatRelativeTime(date)

    default:
      return date.toLocaleString('zh-CN')
  }
}

/**
 * 截断文本
 * @param text - 原始文本
 * @param maxLength - 最大长度
 * @param ellipsis - 省略符号
 * @returns 截断后的文本
 */
export function truncateText(
  text: string,
  maxLength: number,
  ellipsis: string = '...'
): string {
  if (text.length <= maxLength) {
    return text
  }
  return text.slice(0, maxLength - ellipsis.length) + ellipsis
}

/**
 * 高亮搜索关键词
 * @param text - 原始文本
 * @param keyword - 搜索关键词
 * @returns 包含高亮标记的文本
 */
export function highlightKeyword(text: string, keyword: string): string {
  if (!keyword) {
    return text
  }

  const regex = new RegExp(`(${escapeRegExp(keyword)})`, 'gi')
  return text.replace(regex, '<mark>$1</mark>')
}

/**
 * 转义正则表达式特殊字符
 * @param str - 原始字符串
 * @returns 转义后的字符串
 */
export function escapeRegExp(str: string): string {
  return str.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

/**
 * 格式化文件大小
 * @param bytes - 字节数
 * @returns 格式化后的文件大小字符串
 */
export function formatFileSize(bytes: number): string {
  if (bytes === 0) return '0 B'

  const k = 1024
  const sizes = ['B', 'KB', 'MB', 'GB', 'TB']
  const i = Math.floor(Math.log(bytes) / Math.log(k))

  return `${parseFloat((bytes / Math.pow(k, i)).toFixed(2))} ${sizes[i]}`
}

/**
 * 格式化数字（添加千分位分隔符）
 * @param num - 数字
 * @returns 格式化后的数字字符串
 */
export function formatNumber(num: number): string {
  return num.toLocaleString('zh-CN')
}

/**
 * 格式化时间戳（用于消息显示）
 * @param timestamp - ISO时间戳字符串
 * @returns 格式化后的时间字符串
 */
export function formatTimestamp(timestamp: string): string {
  return formatDate(timestamp, 'relative')
}
