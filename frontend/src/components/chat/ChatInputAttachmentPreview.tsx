/** 附件预览组件 */

import {
  AlertCircle,
  File as FileIcon,
  Image as ImageIcon,
  Loader2,
  X,
} from '@/assets/icons'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'
import { formatFileSize } from '@/utils/format'
import type { Attachment, PendingFile } from './types'

/** 附件预览：附件/在途上传文件两态同款渲染（缩略图 + 文件信息 + 上传状态 + 删除） */
export const AttachmentPreview = ({
  attachment,
  onRemove,
}: {
  attachment: Attachment | PendingFile
  onRemove: () => void
}) => {
  const isPendingFile = 'file' in attachment
  const isImage = isPendingFile
    ? attachment.file.type.startsWith('image/')
    : attachment.type?.startsWith('image/')
  const status = isPendingFile ? attachment.status : (attachment as Attachment).status
  const fileName = isPendingFile ? attachment.file.name : attachment.name
  const fileSize = isPendingFile ? attachment.file.size : attachment.size
  const previewUrl = isPendingFile ? attachment.previewUrl : attachment.previewUrl

  return (
    <div
      className={cn(
        'group relative flex items-center gap-2 rounded-xl p-2 transition-all duration-200',
        status === 'error'
          ? 'bg-destructive/10 border-destructive/50 border'
          : 'bg-muted/50 border-border/30 hover:border-border/50 border hover:shadow-sm',
      )}
    >
      {/* 预览图标/缩略图 */}
      {previewUrl ? (
        <img src={previewUrl} alt={fileName} className="h-10 w-10 rounded-lg object-cover" />
      ) : (
        <div className="bg-background/80 flex h-10 w-10 items-center justify-center rounded-lg">
          {isImage ? (
            <ImageIcon className="text-muted-foreground h-5 w-5" />
          ) : (
            <FileIcon className="text-muted-foreground h-5 w-5" />
          )}
        </div>
      )}

      {/* 文件信息 */}
      <div className="min-w-0 flex-1">
        <div className="truncate text-sm font-medium">{fileName}</div>
        <div className="text-muted-foreground text-xs">{formatFileSize(fileSize)}</div>
      </div>

      {/* 上传状态 */}
      {status === 'uploading' && <Loader2 className="text-primary h-icon-md w-icon-md animate-spin" />}
      {status === 'error' && <AlertCircle className="text-destructive h-icon-md w-icon-md" />}

      {/* 删除按钮 */}
      <Button
        variant="ghost"
        size="sm"
        className="hover:bg-destructive/10 hover:text-destructive h-6 w-6 rounded-lg p-0 opacity-100 md:opacity-0 md:group-hover:opacity-100"
        onClick={onRemove}
        disabled={status === 'uploading'}
        aria-label={`移除附件 ${fileName}`}
      >
        <X className="h-icon-md w-icon-md" />
      </Button>
    </div>
  )
}
