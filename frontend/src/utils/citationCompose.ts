/**
 * 引用/评审条目组装（2026-11-05 评审引用交互阶段一，件二 MessageActions 与
 * 件三 CodeEditor 共用真源，禁复制）。设计真值 =
 * docs/working/调研_评审引用交互与默认模型显示_20261104.md §6.1-2：
 * 合成条目形态 = markdown 引用块（`>` 摘录 + 评审说明），摘录为定格快照。
 */

import { formatDate } from './format'

/** 引用摘录截断阈值（约 400 字，超出截断加省略号） */
export const CITATION_QUOTE_MAX_CHARS = 400

/** 块引用组装入参 */
export interface BlockQuoteInput {
  /** 被引正文（定格快照） */
  content: string
  /** 来源标签（消息=角色；代码=文件路径信息） */
  sourceLabel: string
  /** 来源时间（ISO 串；消息角色注记带时间，代码引用无） */
  timestamp?: string
}

/** 来源注记文本：来源标签（+ 可选时间） */
function formatCitationSource(sourceLabel: string, timestamp?: string): string {
  if (!timestamp) return sourceLabel
  return `${sourceLabel} · ${formatDate(timestamp, 'datetime')}`
}

/** 摘录文本截断：超阈值取前 400 字加省略号（先截断再逐行加前缀）。
 *  双消费单真源：buildBlockQuote（发送装配）与 R5 卡片摘录快照（store 入口
 *  addCitation）同阈值同形态——卡片摘录截断后发送产物仍与旧 requestInsert
 *  通道等价。 */
export function truncateCitationExcerpt(content: string): string {
  const trimmed = content.trim()
  if (trimmed.length <= CITATION_QUOTE_MAX_CHARS) return trimmed
  return `${trimmed.slice(0, CITATION_QUOTE_MAX_CHARS)}…`
}

/**
 * markdown 块引用：首行来源注记，正文逐行 `> ` 前缀。
 * 空白正文仅产出来源注记行（不产出空块）。
 */
export function buildBlockQuote(input: BlockQuoteInput): string {
  const source = formatCitationSource(input.sourceLabel, input.timestamp)
  const body = truncateCitationExcerpt(input.content)
  if (!body) return `> 【${source}】`
  return [`> 【${source}】`, ...body.split('\n').map((line) => `> ${line}`)].join('\n')
}

/** 合成条目入参：块引用 + 可选评审说明 */
export interface CitationEntryInput extends BlockQuoteInput {
  /** 评审意见（空/纯空白 = 等价纯引用） */
  comment?: string
}

/**
 * 引用+评审合成条目：`> 引用文本` + 空行 + 评审说明，一次插入；
 * 意见为空时等价纯引用块。
 */
export function composeCitationEntry(input: CitationEntryInput): string {
  const quote = buildBlockQuote(input)
  const comment = input.comment?.trim()
  return comment ? `${quote}\n\n${comment}` : quote
}
