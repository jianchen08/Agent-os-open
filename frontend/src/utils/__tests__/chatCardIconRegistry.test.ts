/**
 * 功能测试：chatCardIconRegistry —— chat_card.icon 字符串解析为图标组件（G4）
 *
 * 功能点：插件用语义字符串声明图标（"terminal"/"file"/"globe"...），解析器返回对应组件，
 * 未命中走默认（不空白）。大小写不敏感。
 */

import { describe, expect, it } from 'vitest'
import { FileEdit, FileText, Globe, Terminal } from '@/assets/icons'
import { resolveChatCardIcon } from '@/utils/chatCardIconRegistry'

describe('功能点：resolveChatCardIcon 字符串→图标组件', () => {
  it('常见语义名命中对应图标', () => {
    expect(resolveChatCardIcon('terminal')).toBe(Terminal)
    expect(resolveChatCardIcon('file')).toBe(FileText)
    expect(resolveChatCardIcon('edit')).toBe(FileEdit)
    expect(resolveChatCardIcon('globe')).toBe(Globe)
  })

  it('多别名指向同一图标（file_read/file/write → 文件类）', () => {
    expect(resolveChatCardIcon('file_read')).toBe(FileText)
    expect(resolveChatCardIcon('write')).toBe(FileEdit)
    expect(resolveChatCardIcon('bash')).toBe(Terminal)
  })

  it('大小写不敏感', () => {
    expect(resolveChatCardIcon('TERMINAL')).toBe(Terminal)
    expect(resolveChatCardIcon('File')).toBe(FileText)
  })

  it('未知名 / 缺省 → 默认图标（不返回 null，保证 customIcon 有值）', () => {
    expect(resolveChatCardIcon('unknown_xyz')).toBeDefined()
    expect(resolveChatCardIcon(undefined)).toBeDefined()
    expect(resolveChatCardIcon('')).toBeDefined()
  })
})
