/** @feature FP-0.2.四 文本选区工具 | @ci: frontend-test */
/**
 * 文本选区 / 拖拽判定工具测试
 *
 * 覆盖：选区存在性与归属根节点判定、指针位移阈值判定、交互元素目标判定。
 * 服务对象：通知条目「拖选文字被误当点击」的甄别逻辑。
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  DRAG_THRESHOLD_PX,
  hasDraggedBeyond,
  hasTextSelectionWithin,
  isInteractiveTarget,
} from '@/utils/textSelection'

/** 在元素内容上建立非折叠选区 */
function selectContents(element: Element): void {
  const range = document.createRange()
  range.selectNodeContents(element)
  const selection = window.getSelection()
  selection?.removeAllRanges()
  selection?.addRange(range)
}

afterEach(() => {
  window.getSelection()?.removeAllRanges()
  vi.restoreAllMocks()
})

describe('hasTextSelectionWithin', () => {
  it('无选区时为 false', () => {
    expect(hasTextSelectionWithin()).toBe(false)
  })

  it('选区落在 root 内时为 true', () => {
    const root = document.createElement('div')
    const text = document.createElement('span')
    text.textContent = '可拖选文本'
    root.appendChild(text)
    document.body.appendChild(root)
    selectContents(text)

    expect(hasTextSelectionWithin(root)).toBe(true)
    expect(hasTextSelectionWithin()).toBe(true)
  })

  it('选区落在 root 外时为 false', () => {
    const root = document.createElement('div')
    const other = document.createElement('span')
    other.textContent = '别处文本'
    document.body.appendChild(root)
    document.body.appendChild(other)
    selectContents(other)

    expect(hasTextSelectionWithin(root)).toBe(false)
  })

  it('浏览器不支持选区 API 时安全降级为 false', () => {
    vi.spyOn(window, 'getSelection').mockReturnValue(null as unknown as Selection)
    expect(hasTextSelectionWithin()).toBe(false)
  })
})

describe('hasDraggedBeyond', () => {
  it('无起点记录时为 false', () => {
    expect(hasDraggedBeyond(null, { clientX: 100, clientY: 100 })).toBe(false)
  })

  it('位移未超阈值时为 false', () => {
    expect(hasDraggedBeyond({ x: 100, y: 100 }, { clientX: 102, clientY: 103 })).toBe(false)
  })

  it('位移超阈值时为 true', () => {
    expect(hasDraggedBeyond({ x: 100, y: 100 }, { clientX: 100, clientY: 100 + DRAG_THRESHOLD_PX + 1 })).toBe(true)
  })

  it('支持自定义阈值', () => {
    expect(hasDraggedBeyond({ x: 0, y: 0 }, { clientX: 8, clientY: 0 }, 10)).toBe(false)
    expect(hasDraggedBeyond({ x: 0, y: 0 }, { clientX: 8, clientY: 0 }, 5)).toBe(true)
  })
})

describe('isInteractiveTarget', () => {
  it('空目标与非元素目标为 false', () => {
    expect(isInteractiveTarget(null)).toBe(false)
    expect(isInteractiveTarget({} as unknown as EventTarget)).toBe(false)
  })

  it('普通文本元素为 false', () => {
    const span = document.createElement('span')
    expect(isInteractiveTarget(span)).toBe(false)
  })

  it('按钮/链接及其子元素为 true', () => {
    const button = document.createElement('button')
    const buttonChild = document.createElement('span')
    button.appendChild(buttonChild)
    const link = document.createElement('a')
    link.setAttribute('href', '#')

    expect(isInteractiveTarget(button)).toBe(true)
    expect(isInteractiveTarget(buttonChild)).toBe(true)
    expect(isInteractiveTarget(link)).toBe(true)
  })

  it('role=button 的元素为 true', () => {
    const div = document.createElement('div')
    div.setAttribute('role', 'button')
    expect(isInteractiveTarget(div)).toBe(true)
  })
})
