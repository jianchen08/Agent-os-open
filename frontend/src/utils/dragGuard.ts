/**
 * 拖拽守卫：宿主文档与内嵌文档（iframe/webview）的事件边界
 *
 * 内嵌文档会吞掉宿主的指针事件：指针进入内嵌区域后，pointermove/pointerup
 * 派发给内嵌文档，宿主收不到。于是「按下拖拽、抬起时指针落在内嵌文档上」的
 * 场景里，宿主永远等不到抬起事件——拖拽会话无法结束，被拖对象此后会继续
 * 跟随鼠标移动（用户可见：松开左键后工作区仍在动；侧栏无内嵌文档故不复现）。
 * Chromium 实测：setPointerCapture 跨不过内嵌文档边界，抬起事件照样丢。
 *
 * 两道守卫（缺一不可）：
 * 1) 拖拽期间把内嵌文档置为 pointer-events:none，指针事件改由宿主文档承接
 *    ——拖拽途经内嵌内容时不再跟丢，抬起事件也必然回到宿主；
 * 2) 移动事件里 buttons===0 = 按键已松开（抬起事件可能被吞）→ 判定拖拽
 *    已结束并收尾，保证拖拽会话一定会结束。
 */

/** 会吞掉宿主指针事件的内嵌文档（Electron 的 webview 亦在内） */
const EMBEDDED_SELECTOR = 'iframe, webview, embed, object'

/**
 * 拖拽期间屏蔽内嵌文档的指针事件，返回还原函数。
 * 还原函数必须被调用（拖拽收尾时）——否则内嵌内容此后不可点。
 */
export function suppressEmbeddedPointerEvents(): () => void {
  const nodes = Array.from(document.querySelectorAll<HTMLElement>(EMBEDDED_SELECTOR))
  const saved = nodes.map((el) => ({ el, pointerEvents: el.style.pointerEvents }))
  for (const { el } of saved) {
    el.style.pointerEvents = 'none'
  }
  let restored = false
  return () => {
    if (restored) return
    restored = true
    for (const { el, pointerEvents } of saved) {
      el.style.pointerEvents = pointerEvents
    }
  }
}

/**
 * 拖拽是否已结束（按键已松开）。
 * 抬起事件可能被内嵌文档吞掉，宿主只能从移动事件反推：没有按键按下 = 已抬起。
 */
export function isDragReleased(event: { buttons: number }): boolean {
  return event.buttons === 0
}
