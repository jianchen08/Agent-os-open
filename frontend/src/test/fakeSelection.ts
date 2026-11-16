/**
 * 测试共享：jsdom 假 Selection 工具（jsdom 无真实选区 API）。
 * 单源 = 本文件；CodeEditor 选中引用系列测试共用（禁测试 helper 复制漂移）。
 */
import { vi } from 'vitest'

/** 在容器内对 `needle` 构造一个假 Selection。
 * range 暴露真实文本节点与偏移——被测代码用它回算行号；toString 返回
 * 容器内真实选中的子串（与 DOM 偏移一致，不是任意捏造值）。 */
export function fakeSelect(container: Element, needle: string): Selection {
  const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT)
  let node = walker.nextNode() as Text | null
  while (node) {
    const idx = node.textContent?.indexOf(needle) ?? -1
    if (idx >= 0) {
      const selected = node.textContent!.substring(idx, idx + needle.length)
      const rect = {
        left: 10, top: 20, right: 60, bottom: 32, width: 50, height: 12,
        x: 10, y: 20, toJSON: () => ({}),
      }
      const range = {
        startContainer: node,
        startOffset: idx,
        endContainer: node,
        endOffset: idx + needle.length,
        collapsed: false,
        commonAncestorContainer: node,
        getBoundingClientRect: () => rect,
      }
      return {
        isCollapsed: false,
        toString: () => selected,
        rangeCount: 1,
        getRangeAt: () => range,
        removeAllRanges: () => {},
      } as unknown as Selection
    }
    node = walker.nextNode() as Text | null
  }
  throw new Error(`text not found in container: ${needle}`)
}

/** window.getSelection 打桩到容器内指定文本的假选区 */
export function mockSelection(container: Element, needle: string): void {
  vi.spyOn(window, 'getSelection').mockReturnValue(fakeSelect(container, needle))
}
