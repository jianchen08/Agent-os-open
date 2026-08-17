/**
 * 全局图片预览灯箱（chat_card actions on_click preview_image 协议宿主，
 * widget 化 T3）。
 *
 * main.tsx 挂载一次；toolCardRegistry 的全局回调注册到本组件 state。
 * 点击遮罩/ESC 关闭。
 */
import { useEffect, useState } from 'react'
import { registerGlobalImagePreviewCallback } from '@/utils/toolCardRegistry'

export function ImagePreviewHost() {
  const [src, setSrc] = useState<string | null>(null)

  useEffect(() => {
    registerGlobalImagePreviewCallback(setSrc)
    return () => {
      // 卸载时恢复缺省兜底（新标签打开）
      registerGlobalImagePreviewCallback(null)
    }
  }, [])

  useEffect(() => {
    if (!src) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setSrc(null)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [src])

  if (!src) return null

  return (
    <div
      className="bg-[var(--overlay-strong)] fixed inset-0 z-50 flex cursor-zoom-out items-center justify-center p-6"
      onClick={() => setSrc(null)}
      role="dialog"
      aria-modal="true"
      aria-label="图片预览"
    >
      <img
        src={src}
        alt="大图预览"
        className="max-h-[85vh] max-w-[90vw] rounded object-contain shadow-2xl"
      />
    </div>
  )
}
