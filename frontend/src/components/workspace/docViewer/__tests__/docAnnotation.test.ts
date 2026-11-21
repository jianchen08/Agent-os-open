/** @feature FP-0.2.四 前端Schema（文档批注） @vision V6 可即用 @ci frontend-test */
/**
 * 文档批注锚定测试（任务现场批 5，纯函数层）。
 *
 * 判据（方案 §七 批 5-1/5-4）：选区能换算成文档字符偏移（落成 TextSelectionTarget）；
 * 选区不在文档内/空选不给假锚点；偏移能还原定位；文档正文只读不改写（函数无写面）。
 */
import { describe, expect, it, beforeEach, afterEach, vi } from 'vitest'
import {
  annotationRects,
  collectTextSpans,
  documentTextLength,
  locateOffset,
  selectionToTarget,
} from '@/components/workspace/docViewer/docAnnotation'

describe('docAnnotation — 选区与偏移', () => {
  let host: HTMLDivElement

  beforeEach(() => {
    host = document.createElement('div')
    host.innerHTML = '<p>第一段文字</p><p>second paragraph</p>'
    document.body.appendChild(host)
  })

  afterEach(() => {
    host.remove()
  })

  it('收集文本节点：按文档序累计长度，跳过空文本', () => {
    const spans = collectTextSpans(host)
    expect(spans).toHaveLength(2)
    expect(spans[0]!.start).toBe(0)
    expect(spans[0]!.length).toBe(5) // 第一段文字
    expect(spans[1]!.start).toBe(5)
    expect(documentTextLength(host)).toBe(5 + 16)
  })

  it('偏移还原定位：跨两个文本节点边界都能落点', () => {
    const spans = collectTextSpans(host)
    const first = locateOffset(spans, 2)
    expect(first?.node.textContent).toBe('第一段文字')
    expect(first?.offsetInNode).toBe(2)
    const second = locateOffset(spans, 7)
    expect(second?.node.textContent).toBe('second paragraph')
    expect(second?.offsetInNode).toBe(2)
  })

  it('选区 → 字符偏移（选中第一段前两个字）', () => {
    const firstText = host.querySelector('p')!.firstChild as Text
    const range = document.createRange()
    range.setStart(firstText, 0)
    range.setEnd(firstText, 2)
    const selection = window.getSelection()!
    selection.removeAllRanges()
    selection.addRange(range)

    const target = selectionToTarget(host, selection)
    expect(target).toEqual({ startOffset: 0, endOffset: 2, selectedText: '第一' })
    selection.removeAllRanges()
  })

  it('空选 / 选区在容器外 / 纯空白：不给假锚点', () => {
    const selection = window.getSelection()!
    selection.removeAllRanges()
    // 折叠选区
    const text = host.querySelector('p')!.firstChild as Text
    const collapsed = document.createRange()
    collapsed.setStart(text, 1)
    collapsed.collapse(true)
    selection.addRange(collapsed)
    expect(selectionToTarget(host, selection)).toBeUndefined()

    // 总长恰等（尾部边界）落在最后一个文本节点末尾（locateOffset 尾部分支）
    const spans = collectTextSpans(host)
    const tail = locateOffset(spans, documentTextLength(host))
    expect(tail?.node).toBe(spans[spans.length - 1]!.node)
    expect(tail?.offsetInNode).toBe(spans[spans.length - 1]!.length)

    // 选区在容器外
    const outside = document.createElement('div')
    outside.textContent = '外部文字'
    document.body.appendChild(outside)
    const outsideRange = document.createRange()
    outsideRange.selectNodeContents(outside)
    selection.removeAllRanges()
    selection.addRange(outsideRange)
    expect(selectionToTarget(host, selection)).toBeUndefined()
    outside.remove()
    selection.removeAllRanges()
  })

  it('高亮矩形：无布局环境（jsdom 无 rect）返回空数组，不抛错', () => {
    expect(annotationRects(host, 0, 3)).toEqual([])
  })

  it('越界偏移不落点（不给错位高亮）', () => {
    const spans = collectTextSpans(host)
    expect(locateOffset(spans, -1)).toBeUndefined()
    expect(locateOffset(spans, 9999)).toBeUndefined()
  })
})

describe('docAnnotation — 元素容器定位分支补缺', () => {
  let host: HTMLDivElement

  beforeEach(() => {
    host = document.createElement('div')
    // 两段各包一层 <p>，构造「元素容器」与嵌套首个/末个文本节点形态
    host.innerHTML = '<p><span>甲段文字</span></p><p><b>乙段</b>尾巴</p>'
    document.body.appendChild(host)
  })

  afterEach(() => {
    host.remove()
  })

  it('offsetOfPoint 经 selectionToTarget：选区锚在元素容器上按子节点序换算', () => {
    const firstP = host.querySelector('p')!
    const selection = document.getSelection()!
    // 锚点 1 = 第二个 <p>（其首个文本节点「乙段」的起点 = 5）
    const anchorRange = document.createRange()
    anchorRange.setStart(firstP, 1)
    const focusRange = document.createRange()
    focusRange.setStart(host.querySelector('b')!.firstChild!, 2)
    const range = document.createRange()
    range.setStart(anchorRange.startContainer, anchorRange.startOffset)
    range.setEnd(focusRange.startContainer, focusRange.startOffset)
    selection.removeAllRanges()
    selection.addRange(range)
    // 「甲段文字」4 字；「乙段」起点 = 4，focus offset 2 → end = 6
    const target = selectionToTarget(host, selection)
    expect(target).toMatchObject({ startOffset: 4, endOffset: 6 })
    expect(target?.selectedText).toBe('乙段')
    selection.removeAllRanges()
  })

  it('元素容器子序越界：落最后文本节点末尾（尾巴 = 全文末点）', () => {
    const lastP = host.querySelectorAll('p')[1]!
    const selection = document.getSelection()!
    const range = document.createRange()
    // 第二个 <p> 子序 = 子节点数（合法的「末子之后」位）→ 末点 = 「尾巴」之后（总长 9）
    range.setStart(lastP.firstChild!.firstChild!, 0)
    range.setEnd(lastP, lastP.childNodes.length)
    selection.removeAllRanges()
    selection.addRange(range)
    const target = selectionToTarget(host, selection)
    // 总长 = 甲段文字(4) + 乙段(2) + 尾巴(2) = 8；末点 = 8
    expect(target).toMatchObject({ startOffset: 4, endOffset: 8 })
    selection.removeAllRanges()
  })

  it('选区锚在元素容器子序 0：取该子首个后代文本起点（span 内嵌走 walker）', () => {
    const firstP = host.querySelector('p')!
    const selection = document.getSelection()!
    const range = document.createRange()
    // startContainer = <p>（元素容器）子序 0 → <span> 内首文本起点 = 0
    range.setStart(firstP, 0)
    range.setEnd(host.querySelectorAll('p')[1]!.firstChild!.firstChild!, 2)
    selection.removeAllRanges()
    selection.addRange(range)
    const target = selectionToTarget(host, selection)
    // 起点取子节点（<span>）的首文本起点 0；终点 = 乙段起点 4 + 2 = 6
    expect(target).toMatchObject({ startOffset: 0, endOffset: 6 })
    selection.removeAllRanges()
  })
})

describe('docAnnotation — annotationRects 布局分支', () => {
  let host: HTMLDivElement

  beforeEach(() => {
    host = document.createElement('div')
    host.innerHTML = '<p>第一段文字</p>'
    document.body.appendChild(host)
  })

  afterEach(() => {
    host.remove()
    vi.restoreAllMocks()
  })

  it('有布局（root/Range rect 就位）产出相对坐标矩形；零尺寸矩形跳过', () => {
    vi.spyOn(host, 'getBoundingClientRect').mockReturnValue({
      left: 100, top: 50, width: 200, height: 100, right: 300, bottom: 150, x: 100, y: 50,
      toJSON: () => ({}),
    } as DOMRect)
    const rectsStub = [
      { left: 100, top: 50, width: 100, height: 20, right: 200, bottom: 70, x: 100, y: 50, toJSON: () => ({}) },
      { left: 100, top: 90, width: 0, height: 0, right: 100, bottom: 90, x: 100, y: 90, toJSON: () => ({}) },
    ] as DOMRect[]
    // jsdom 的 Range 无 getClientRects（生产代码按能力探测降级）——注入桩后走真布局分支
    const original = Range.prototype.getClientRects
    Range.prototype.getClientRects = () => rectsStub as unknown as DOMRectList
    try {
      const rects = annotationRects(host, 0, 5)
      // 零尺寸第二块被跳过；第一块换算为相对坐标 (0,0,50%,20%)
      expect(rects).toEqual([{ left: 0, top: 0, width: 0.5, height: 0.2 }])
    } finally {
      Range.prototype.getClientRects = original
    }
  })

  it('root 零尺寸（不可见）返回空数组', () => {
    vi.spyOn(host, 'getBoundingClientRect').mockReturnValue({
      left: 0, top: 0, width: 0, height: 0, right: 0, bottom: 0, x: 0, y: 0, toJSON: () => ({}),
    } as DOMRect)
    expect(annotationRects(host, 0, 5)).toEqual([])
  })
})
