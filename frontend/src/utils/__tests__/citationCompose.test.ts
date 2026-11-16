// @feature FP-0.2.评审引用交互 件二：引用/评审条目组装 util | @ci: frontend-test
/**
 * citationCompose 纯函数测试（件二 MessageActions / 件三 CodeEditor 共用，
 * 禁复制）。设计真值 = 调研_评审引用交互与默认模型显示_20261104.md §6.1-2：
 * 合成条目形态 = markdown 引用块（`>` 摘录 + 评审说明），摘录定格快照。
 */

import { describe, expect, it } from 'vitest'
import {
  buildBlockQuote,
  composeCitationEntry,
  CITATION_QUOTE_MAX_CHARS,
} from '../citationCompose'

describe('buildBlockQuote 块引用格式', () => {
  it('逐行 `> ` 前缀，首行为来源注记（角色+时间）', () => {
    const quote = buildBlockQuote({
      content: '第一行\n第二行',
      sourceLabel: 'assistant',
      timestamp: '2026-11-05T06:30:00Z',
    })
    const lines = quote.split('\n')
    expect(lines[0]).toMatch(/^> 【assistant · \d{4}\/\d{2}\/\d{2} \d{2}:\d{2}】$/)
    expect(lines[1]).toBe('> 第一行')
    expect(lines[2]).toBe('> 第二行')
  })

  it('无时间戳 → 来源注记仅来源标签', () => {
    const quote = buildBlockQuote({ content: '正文', sourceLabel: 'src/main.py' })
    expect(quote.split('\n')[0]).toBe('> 【src/main.py】')
    expect(quote.split('\n')[1]).toBe('> 正文')
  })

  it('正文超约 400 字截断加省略号（401 字 → 400 字 + …）', () => {
    const long = 'a'.repeat(CITATION_QUOTE_MAX_CHARS + 1)
    const quote = buildBlockQuote({ content: long, sourceLabel: 'assistant' })
    expect(quote).toContain('…')
    const bodyLine = quote.split('\n')[1]
    expect(bodyLine).toBe(`> ${'a'.repeat(CITATION_QUOTE_MAX_CHARS)}…`)
  })

  it('恰在阈值内不截断（400 字 → 无省略号）', () => {
    const exact = 'b'.repeat(CITATION_QUOTE_MAX_CHARS)
    const quote = buildBlockQuote({ content: exact, sourceLabel: 'assistant' })
    expect(quote).not.toContain('…')
    expect(quote.split('\n')[1]).toBe(`> ${exact}`)
  })

  it('多行长文截断发生在整体拼接前（先截断再逐行加前缀）', () => {
    const content = `${'x'.repeat(CITATION_QUOTE_MAX_CHARS)}尾\n后续行`
    const quote = buildBlockQuote({ content, sourceLabel: 'f.py' })
    // 截断后的内容只剩一行（… 在第 400 字处），「后续行」不得出现
    expect(quote).not.toContain('后续行')
    expect(quote).toContain('…')
  })

  it('空内容仍产出来源注记行（引用空消息不产出空块）', () => {
    const quote = buildBlockQuote({ content: '   ', sourceLabel: 'assistant' })
    expect(quote).toBe('> 【assistant】')
  })
})

describe('composeCitationEntry 合成条目', () => {
  const quoteBase = {
    content: '被引正文',
    sourceLabel: 'assistant',
    timestamp: '2026-11-05T06:30:00Z',
  }

  it('引用块 + 空行 + 评审说明一次成型', () => {
    const entry = composeCitationEntry({ ...quoteBase, comment: '这里逻辑有问题' })
    const quote = buildBlockQuote(quoteBase)
    expect(entry).toBe(`${quote}\n\n这里逻辑有问题`)
  })

  it('意见为空/纯空白 → 等价纯引用', () => {
    expect(composeCitationEntry(quoteBase)).toBe(buildBlockQuote(quoteBase))
    expect(composeCitationEntry({ ...quoteBase, comment: '   ' })).toBe(
      buildBlockQuote(quoteBase),
    )
  })
})
