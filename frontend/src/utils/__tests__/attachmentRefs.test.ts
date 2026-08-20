/** @feature FP-0.2.四 前端Schema @vision V6 可即用 @ci frontend-test */
/**
 * appendAttachmentRefs 单元测试——附件索引以 markdown 引用并入消息正文
 * （ADR 2026-08-21：索引随 content 携带，内核零改动）。
 */
import { describe, expect, it } from 'vitest'
import { appendAttachmentRefs } from '../attachmentRefs'

describe('appendAttachmentRefs：附件引用并入 content', () => {
  it('无附件时原样返回（零改动）', () => {
    expect(appendAttachmentRefs('你好')).toBe('你好')
    expect(appendAttachmentRefs('你好', [])).toBe('你好')
    expect(appendAttachmentRefs('你好', null)).toBe('你好')
  })

  it('无 url 的附件被跳过（上传未完成/坏数据不产引用）', () => {
    expect(appendAttachmentRefs('你好', [{ name: 'a.png', type: 'image/png' }])).toBe('你好')
  })

  it('图片附件产出 markdown 图片引用（!前缀），与正文空行分隔', () => {
    const out = appendAttachmentRefs('看看', [
      { name: 'cat.png', type: 'image/png', url: '/uploads/cat.png' },
    ])
    expect(out).toBe('看看\n\n![cat.png](/uploads/cat.png)')
  })

  it('非图片附件产出普通链接引用（无!前缀）', () => {
    const out = appendAttachmentRefs('', [
      { name: 'report.pdf', type: 'application/pdf', url: '/uploads/r.pdf' },
    ])
    expect(out).toBe('[report.pdf](/uploads/r.pdf)')
  })

  it('多附件按序追加，图片/文件混排', () => {
    const out = appendAttachmentRefs('正文', [
      { name: 'a.png', type: 'image/png', url: '/uploads/a.png' },
      { name: 'b.pdf', type: 'application/pdf', url: '/uploads/b.pdf' },
      { name: 'c.jpg', type: 'image/jpeg', url: '/uploads/c.jpg' },
    ])
    expect(out).toBe('正文\n\n![a.png](/uploads/a.png)\n\n[b.pdf](/uploads/b.pdf)\n\n![c.jpg](/uploads/c.jpg)')
  })

  it('文件名缺省回退"附件"，方括号剔除（防 markdown 链接语法破损）', () => {
    const out = appendAttachmentRefs('x', [
      { type: 'image/png', url: '/uploads/u.png' },
      { name: 'weird[n].pdf', type: 'application/pdf', url: '/uploads/w.pdf' },
    ])
    expect(out).toContain('![附件](/uploads/u.png)')
    expect(out).toContain('[weirdn.pdf](/uploads/w.pdf)')
    expect(out).not.toContain('weird[n]')
  })

  it('正文仅空白时只产出引用块（不残留空行）', () => {
    const out = appendAttachmentRefs('   ', [
      { name: 'a.png', type: 'image/png', url: '/uploads/a.png' },
    ])
    expect(out).toBe('![a.png](/uploads/a.png)')
  })
})
