/** 已入列附件与在途文件预览行：同一容器 flex-wrap 排布（预览件复用 AttachmentPreview） */

import { AttachmentPreview } from './ChatInputAttachmentPreview'
import type { Attachment, PendingFile } from './types'

interface ComposerAttachmentListProps {
  /** 已入列附件 */
  attachments: Attachment[]
  /** 在途文件（上传中/已完成） */
  pendingFiles: PendingFile[]
  /** 移除条目（预览 URL 回收由 ChatInput handleRemoveAttachment 统一处理） */
  onRemove: (id: string) => void
}

export function ComposerAttachmentList({
  attachments,
  pendingFiles,
  onRemove,
}: ComposerAttachmentListProps) {
  return (
    <div className="flex flex-wrap gap-2 px-3 pt-3 pb-2">
      {attachments.map((attachment) => (
        <AttachmentPreview
          key={attachment.id}
          attachment={attachment}
          onRemove={() => onRemove(attachment.id)}
        />
      ))}
      {pendingFiles.map((pendingFile) => (
        <AttachmentPreview
          key={pendingFile.id}
          attachment={pendingFile}
          onRemove={() => onRemove(pendingFile.id)}
        />
      ))}
    </div>
  )
}
