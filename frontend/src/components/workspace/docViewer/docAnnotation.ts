/**
 * 文档批注锚定（纯函数，任务现场批 5）
 *
 * 渲染件（pdf.js / docx-preview / pptx-preview）只负责把文档变 DOM，**不带批注模型**
 * （方案 §3.6.3 结论：无成熟批注组件可接）。故锚定层自研，但只做三件事：
 *
 * 1. **选区 → 字符偏移**：对容器内文本节点按文档序累计长度，把浏览器 Selection
 *    换算成 `{startOffset, endOffset, selectedText}`（落到既有 `TextSelectionTarget`）
 * 2. **偏移 → 高亮矩形**：从偏移还原 DOM Range，取 `getClientRects()`，转成相对
 *    容器的百分比矩形（复用 `ImageAnnotationView` 的百分比 overlay 思路）
 * 3. **偏移 → 文本节点定位**：给回取原文/复制用
 *
 * 不依赖任何具体渲染件（三格式共用），也不改文档本体（只读边界，方案 §3.6.5）。
 */

/** 支持的文档格式（扩展名） */
export type DocumentFormat = 'pdf' | 'docx' | 'pptx'

/** 扩展名 → 文档格式（不支持的返回 undefined） */
export function detectDocumentFormat(filePath: string): DocumentFormat | undefined {
  const lower = filePath.toLowerCase()
  if (lower.endsWith('.pdf')) return 'pdf'
  if (lower.endsWith('.docx')) return 'docx'
  if (lower.endsWith('.pptx')) return 'pptx'
  return undefined
}

/** 文本节点切片（定位用） */
interface TextNodeSpan {
  node: Text
  /** 该节点在整篇文档文本中的起始偏移 */
  start: number
  /** 该节点文本长度 */
  length: number
}

/** 批注目标（对齐 types/artifact.ts 的 TextSelectionTarget 子集） */
export interface DocumentSelectionTarget {
  startOffset: number
  endOffset: number
  selectedText: string
}

/** 高亮矩形（相对容器的百分比，0~1） */
export interface HighlightRect {
  left: number
  top: number
  width: number
  height: number
}

/** 收集容器内全部非空文本节点（文档序，跳过脚本/样式等不可见节点） */
export function collectTextSpans(root: HTMLElement): TextNodeSpan[] {
  const spans: TextNodeSpan[] = []
  const walker = root.ownerDocument.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode: (node) => {
      const parent = node.parentElement
      if (!parent) return NodeFilter.FILTER_REJECT
      const tag = parent.tagName.toLowerCase()
      if (tag === 'script' || tag === 'style' || tag === 'noscript') return NodeFilter.FILTER_REJECT
      return node.textContent && node.textContent.length > 0
        ? NodeFilter.FILTER_ACCEPT
        : NodeFilter.FILTER_REJECT
    },
  })
  let offset = 0
  let current = walker.nextNode()
  while (current) {
    const text = current as Text
    const length = text.textContent?.length ?? 0
    spans.push({ node: text, start: offset, length })
    offset += length
    current = walker.nextNode()
  }
  return spans
}

/** 文档纯文本总长度（无文本返回 0） */
export function documentTextLength(root: HTMLElement): number {
  const spans = collectTextSpans(root)
  const last = spans[spans.length - 1]
  if (!last) return 0
  return last.start + last.length
}

/** 偏移 → 文本节点（落点节点与该节点内偏移）；越界返回 undefined */
export function locateOffset(
  spans: readonly TextNodeSpan[],
  offset: number,
): { node: Text; offsetInNode: number } | undefined {
  if (offset < 0) return undefined
  for (const span of spans) {
    if (offset <= span.start + span.length) {
      return { node: span.node, offsetInNode: Math.min(offset - span.start, span.length) }
    }
  }
  // 尾部边界（offset === 总长度）落在最后一个节点末尾
  const last = spans[spans.length - 1]
  if (last && offset === last.start + last.length) {
    return { node: last.node, offsetInNode: last.length }
  }
  return undefined
}

/**
 * 浏览器 Selection → 文档字符偏移。
 *
 * 选区不在容器内 / 折叠（空选）/ 无法定位时返回 undefined（不给假批注锚点）。
 */
export function selectionToTarget(
  root: HTMLElement,
  selection: Selection | null,
): DocumentSelectionTarget | undefined {
  if (!selection || selection.rangeCount === 0 || selection.isCollapsed) return undefined
  const range = selection.getRangeAt(0)
  if (!root.contains(range.startContainer) || !root.contains(range.endContainer)) return undefined

  const spans = collectTextSpans(root)
  const start = offsetOfPoint(spans, range.startContainer, range.startOffset)
  const end = offsetOfPoint(spans, range.endContainer, range.endOffset)
  if (start === undefined || end === undefined || end <= start) return undefined

  const selectedText = range.toString()
  if (selectedText.trim() === '') return undefined
  return { startOffset: start, endOffset: end, selectedText }
}

/** DOM 点（容器+节点内偏移）→ 文档字符偏移 */
function offsetOfPoint(
  spans: readonly TextNodeSpan[],
  container: Node,
  offsetInContainer: number,
): number | undefined {
  if (container.nodeType === Node.TEXT_NODE) {
    const span = spans.find((s) => s.node === container)
    return span ? span.start + Math.min(offsetInContainer, span.length) : undefined
  }
  // 元素容器：按子节点序号换算到「该子节点起点」
  const element = container as Element
  const children = Array.from(element.childNodes)
  if (offsetInContainer >= children.length) {
    // 末尾：取最后一个后代文本节点的末尾
    const descendant = lastTextIn(element)
    const span = descendant ? spans.find((s) => s.node === descendant) : undefined
    return span ? span.start + span.length : undefined
  }
  const target = children[offsetInContainer]
  const firstText = target ? firstTextIn(target) : undefined
  const span = firstText ? spans.find((s) => s.node === firstText) : undefined
  return span?.start
}

function firstTextIn(node: Node): Text | undefined {
  if (node.nodeType === Node.TEXT_NODE) return node as Text
  const walker = node.ownerDocument?.createTreeWalker(node, NodeFilter.SHOW_TEXT)
  return (walker?.nextNode() as Text | null) ?? undefined
}

function lastTextIn(node: Node): Text | undefined {
  if (node.nodeType === Node.TEXT_NODE) return node as Text
  const doc = node.ownerDocument
  if (!doc) return undefined
  const walker = doc.createTreeWalker(node, NodeFilter.SHOW_TEXT)
  let last: Text | undefined
  let current = walker.nextNode()
  while (current) {
    last = current as Text
    current = walker.nextNode()
  }
  return last
}

/**
 * 偏移 → 高亮矩形（相对容器百分比）。
 *
 * 依赖 `Range.getClientRects()`；环境不支持（jsdom/隐藏容器）时返回空数组——
 * 调用方不渲染高亮但仍显示侧栏条目（不丢批注）。
 */
export function annotationRects(
  root: HTMLElement,
  startOffset: number,
  endOffset: number,
): HighlightRect[] {
  const spans = collectTextSpans(root)
  const startPoint = locateOffset(spans, startOffset)
  const endPoint = locateOffset(spans, endOffset)
  if (!startPoint || !endPoint) return []

  const range = root.ownerDocument.createRange()
  try {
    range.setStart(startPoint.node, startPoint.offsetInNode)
    range.setEnd(endPoint.node, endPoint.offsetInNode)
  } catch {
    return []
  }

  const rects = typeof range.getClientRects === 'function' ? range.getClientRects() : []
  const rootRect = root.getBoundingClientRect()
  if (!rootRect || rootRect.width === 0 || rootRect.height === 0) return []

  const out: HighlightRect[] = []
  for (const rect of Array.from(rects)) {
    if (rect.width === 0 || rect.height === 0) continue
    out.push({
      left: (rect.left - rootRect.left) / rootRect.width,
      top: (rect.top - rootRect.top) / rootRect.height,
      width: rect.width / rootRect.width,
      height: rect.height / rootRect.height,
    })
  }
  return out
}
