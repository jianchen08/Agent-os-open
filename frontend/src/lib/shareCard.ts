/**
 * 分享卡：把消息卡片 DOM 渲染为 PNG 图片并下载/写剪贴板。
 *
 * DOM→图片经 html-to-image（MIT，npm 依赖）实现——调研报告
 * docs/working/dsh-go移动端本地运行调研与接入工作量_20261112.md §四A
 * 「分享卡 DOM→图片」项的落地件。
 *
 * 接入面：MessageActions 分享按钮 → captureMessageCard(messageEl)。
 * 消息气泡根节点以 [data-testid="message-item"] 标定（MessageItem 渲染面既有）。
 */

import { toPng } from 'html-to-image'

/** 主题令牌内联兜底：html-to-image 克隆 DOM 时外部样式表不随行，
 *  伪元素/变量会丢——用 filter 内联 computed style 由库自行处理（embedWebFonts 默认开）。 */
const CAPTURE_OPTIONS = {
  cacheBust: true,
  pixelRatio: 2,
  backgroundColor: 'rgb(var(--background, 255 255 255))' as unknown as undefined,
}

/** 从消息操作按钮所在位置向上定位消息气泡根节点 */
export function findMessageCard(el: Element | null): HTMLElement | null {
  return el?.closest<HTMLElement>('[data-testid="message-item"]') ?? null
}

/**
 * 把消息气泡 DOM 渲染为 PNG dataURL。
 * 失败抛出原错误（调用方 toast 提示，不静默）。
 */
export async function captureMessageCard(card: HTMLElement): Promise<string> {
  return toPng(card, CAPTURE_OPTIONS)
}

/** 触发浏览器下载 PNG（分享卡落盘） */
export function downloadDataUrl(dataUrl: string, filename: string): void {
  const a = document.createElement('a')
  a.href = dataUrl
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
}

/** PNG dataURL 写剪贴板（支持 ClipboardItem 的环境），不支持时抛错交由调用方降级下载 */
export async function copyPngToClipboard(dataUrl: string): Promise<void> {
  const blob = await (await fetch(dataUrl)).blob()
  if (typeof ClipboardItem === 'undefined' || !navigator.clipboard?.write) {
    throw new Error('clipboard image unsupported')
  }
  await navigator.clipboard.write([new ClipboardItem({ [blob.type]: blob })])
}
