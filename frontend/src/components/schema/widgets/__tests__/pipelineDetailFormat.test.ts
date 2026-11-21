/** @feature FP-0.2.mobile_visual_p1 @ci: frontend-test
 * 任务详情省略格式化单测（移动端视觉 P1-4，MuMu 实测 shots/02）：
 * 短 ID（前 8 位）/ 复合文本 uuid 收短 / ISO → 本地分钟格式 / 非法透传。
 */
import { describe, expect, it } from 'vitest'
import { formatLocalMinute, shortId, shortUuidsInText } from '../pipelineDetailFormat'

describe('shortId — 长 ID 前 8 位', () => {
  it('UUID（36 位）取前 8 位加省略号', () => {
    expect(shortId('dbad2a38-eb2f-4b86-811d-bfc3533a9040')).toBe('dbad2a38…')
  })

  it('12 位管道短 id 取前 8 位加省略号', () => {
    expect(shortId('69a6ead93212')).toBe('69a6ead9…')
  })

  it('≤8 位原样', () => {
    expect(shortId('69a6ead9')).toBe('69a6ead9')
    expect(shortId('')).toBe('')
  })
})

describe('shortUuidsInText — 复合文本 uuid 收短', () => {
  it('归属行「会话名（thread-uuid）」收短 uuid、保留会话名', () => {
    expect(
      shortUuidsInText('e2e47-mobile-smoke（thread-acada2c5-cc79-4328-95cd-b4daed87453d）'),
    ).toBe('e2e47-mobile-smoke（thread-acada2c5…）')
  })

  it('裸 12 位 hex（管道短 id）收短', () => {
    expect(shortUuidsInText('run=69a6ead93212')).toBe('run=69a6ead9…')
  })

  it('普通文本不动', () => {
    expect(shortUuidsInText('已完成（completed）')).toBe('已完成（completed）')
  })
})

describe('formatLocalMinute — ISO → 本地 YYYY-MM-DD HH:mm', () => {
  it('性质断言：本地回解析与原时刻分钟级相等（时区无关）', () => {
    const iso = '2026-11-13T18:09:05.771+00:00' // MuMu 实拍「开始」字段同构
    const out = formatLocalMinute(iso)
    expect(out).toMatch(/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$/)
    const drift = Math.abs(new Date(out).getTime() - new Date(iso).getTime())
    expect(drift).toBeLessThanOrEqual(60_000) // 秒/毫秒截断，分钟级必等
  })

  it('本地构造的确定性用例：2026-01-02 03:04', () => {
    expect(formatLocalMinute(new Date(2026, 0, 2, 3, 4).toISOString())).toBe('2026-01-02 03:04')
  })

  it('非法时间戳原样透传（详情面板不因脏数据丢行）', () => {
    expect(formatLocalMinute('not-a-date')).toBe('not-a-date')
    expect(formatLocalMinute('')).toBe('')
  })
})
