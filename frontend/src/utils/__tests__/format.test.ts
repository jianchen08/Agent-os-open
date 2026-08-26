/**
 * 格式化工具函数测试
 *
 * 覆盖：UTC 时间戳解析（带/不带时区标识、空值）、文件大小格式化
 * （0 B 到 TB 全覆盖）、数字千分位、相对时间戳格式化。
 */

import { describe, expect, it, vi } from 'vitest'
import { formatFileSize, formatNumber, formatTimestamp, parseUTCTimestamp } from '@/utils/format'

describe('parseUTCTimestamp - UTC 时间戳解析', () => {
  it('带 Z 后缀的时间戳直接解析', () => {
    const date = parseUTCTimestamp('2026-01-01T12:00:00Z')
    expect(date.toISOString()).toBe('2026-01-01T12:00:00.000Z')
  })

  it('带时区偏移的时间戳直接解析', () => {
    const date = parseUTCTimestamp('2026-01-01T12:00:00+08:00')
    expect(date.toISOString()).toBe('2026-01-01T04:00:00.000Z')
  })

  it('无时区标识的时间戳按 UTC 补 Z', () => {
    const date = parseUTCTimestamp('2026-01-01T12:00:00')
    expect(date.toISOString()).toBe('2026-01-01T12:00:00.000Z')
  })

  it('空字符串返回当前时间', () => {
    const before = Date.now()
    const date = parseUTCTimestamp('')
    expect(date.getTime()).toBeGreaterThanOrEqual(before)
  })
})

describe('formatFileSize - 文件大小格式化', () => {
  it.each([
    [0, '0 B'],
    [512, '512 B'],
    [1024, '1 KB'],
    [1536, '1.5 KB'],
    [1024 * 1024, '1 MB'],
    [1024 * 1024 * 1024, '1 GB'],
    [1024 ** 4, '1 TB'],
  ])('%s 字节 → %s', (bytes, expected) => {
    expect(formatFileSize(bytes)).toBe(expected)
  })

  it('小数保留两位', () => {
    expect(formatFileSize(1024 * 1.234)).toBe('1.23 KB')
  })
})

describe('formatNumber - 数字千分位', () => {
  it('大数添加千分位分隔符', () => {
    expect(formatNumber(1234567)).toBe('1,234,567')
  })

  it('小数保留原样', () => {
    expect(formatNumber(1234.5)).toBe('1,234.5')
  })
})

describe('formatTimestamp - 相对时间', () => {
  it('一分钟内显示"刚刚"', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-01-01T12:00:00Z'))
    expect(formatTimestamp('2026-01-01T11:59:30Z')).toBe('刚刚')
    vi.useRealTimers()
  })

  it('一小时内显示分钟数', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-01-01T12:00:00Z'))
    expect(formatTimestamp('2026-01-01T11:30:00Z')).toBe('30分钟前')
    vi.useRealTimers()
  })

  it('一天内显示小时数', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-01-01T12:00:00Z'))
    expect(formatTimestamp('2026-01-01T10:00:00Z')).toBe('2小时前')
    vi.useRealTimers()
  })

  it('七天内显示天数', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-01-07T12:00:00Z'))
    expect(formatTimestamp('2026-01-01T12:00:00Z')).toBe('6天前')
    vi.useRealTimers()
  })

  it('满七天（diffDays >= 7）显示日期', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-01-08T12:00:00Z'))
    expect(formatTimestamp('2026-01-01T12:00:00Z')).toBe('2026/01/01')
    vi.useRealTimers()
  })

  it('超过七天显示日期', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-02-01T12:00:00Z'))
    const result = formatTimestamp('2026-01-01T12:00:00Z')
    expect(result).toMatch(/2026/)
    vi.useRealTimers()
  })
})
