/** @feature FP-0.2.四 拖拽护栏工具 | @ci: frontend-test */
/**
 * 拖拽守卫单测（utils/dragGuard）
 *
 * 病灶背景：内嵌文档（iframe/webview）吞掉宿主的 pointerup/pointermove，
 * 拖拽会话无法结束——松开左键后被拖对象继续跟随鼠标移动。
 */
import { afterEach, describe, expect, it } from 'vitest'
import { isDragReleased, suppressEmbeddedPointerEvents } from '../dragGuard'

describe('dragGuard', () => {
  afterEach(() => {
    document.body.innerHTML = ''
  })

  it('拖拽期间屏蔽内嵌文档指针事件；还原后恢复原样', () => {
    document.body.innerHTML = `
      <iframe id="a"></iframe>
      <webview id="b"></webview>
      <embed id="c" />
      <object id="d"></object>
      <div id="plain"></div>
    `
    const iframe = document.getElementById('a') as HTMLElement
    const webview = document.getElementById('b') as HTMLElement
    const embed = document.getElementById('c') as HTMLElement
    const plain = document.getElementById('plain') as HTMLElement

    const restore = suppressEmbeddedPointerEvents()
    expect(iframe.style.pointerEvents).toBe('none')
    expect(webview.style.pointerEvents).toBe('none')
    expect(embed.style.pointerEvents).toBe('none')
    // 非内嵌节点不受影响
    expect(plain.style.pointerEvents).toBe('')

    restore()
    expect(iframe.style.pointerEvents).toBe('')
    expect(webview.style.pointerEvents).toBe('')
    expect(embed.style.pointerEvents).toBe('')
  })

  it('还原保留节点原有 pointer-events 取值，且可重复调用', () => {
    document.body.innerHTML = '<iframe id="a" style="pointer-events: auto"></iframe>'
    const iframe = document.getElementById('a') as HTMLElement

    const restore = suppressEmbeddedPointerEvents()
    expect(iframe.style.pointerEvents).toBe('none')
    restore()
    expect(iframe.style.pointerEvents).toBe('auto')
    // 重复还原不改变结果（幂等）
    restore()
    expect(iframe.style.pointerEvents).toBe('auto')
  })

  it('isDragReleased：按键已松开（buttons=0）判为拖拽结束', () => {
    expect(isDragReleased({ buttons: 0 })).toBe(true)
    expect(isDragReleased({ buttons: 1 })).toBe(false)
    expect(isDragReleased({ buttons: 5 })).toBe(false)
  })
})
