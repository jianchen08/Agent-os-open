/**
 * markdown 图片附件组件（2026-09-08 附件解析失败显式化）。
 *
 * 消息 content 里的附件图片引用（![f](/uploads/x.png)）由此渲染：
 * - 成功：lobehub Image 原样渲染（与既有 mdx img 路径同款样式/预览，行为不变）；
 * - 加载失败（文件被清理/404 等）：显式化为附件失败卡——失败说明 + 附件名，
 *   不再停留为无文字的占位图（用户裁定：前端要说明附件丢了）。
 */
import { Image } from '@lobehub/ui'
import { useState, type CSSProperties, type FC } from 'react'
import { FileWarning } from '@/assets/icons'
import { cn } from '@/lib/utils'

export interface AttachmentImageProps {
  src?: string
  alt?: string
  className?: string
  style?: CSSProperties
  width?: number | string
  height?: number | string
}

export const AttachmentImage: FC<AttachmentImageProps> = ({
  src,
  alt,
  className,
  style,
  width,
  height,
}) => {
  // 失败态锚定触发失败的 src：src 变化（重发/切换附件）自动复位，无需 effect
  const [failedSrc, setFailedSrc] = useState<string | null>(null)
  const failed = failedSrc !== null && failedSrc === src

  if (failed) {
    return (
      <div
        data-testid="attachment-image-failed"
        data-src={src}
        className={cn(
          'border-status-error/40 bg-status-error/5 flex max-w-full items-center gap-2 rounded-lg border px-3 py-2',
          className,
        )}
      >
        <FileWarning className="text-status-error h-icon-md w-icon-md shrink-0" />
        <div className="flex min-w-0 flex-col">
          <span className="text-status-error text-sm font-medium">附件加载失败</span>
          <span className="text-muted-foreground truncate text-xs">{alt || src || '未知来源'}</span>
        </div>
      </div>
    )
  }

  return (
    <Image
      src={src}
      alt={alt ?? 'img'}
      className={className}
      width={width}
      height={height}
      onError={() => setFailedSrc(src ?? '')}
      style={{
        borderRadius: 'calc(var(--lobe-markdown-border-radius) * 1px)',
        marginBlock: 'calc(var(--lobe-markdown-margin-multiple) * 1em)',
        ...style,
      }}
    />
  )
}
