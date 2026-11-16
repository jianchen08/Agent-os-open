/**
 * 多端目录树选择模态框——工作空间选择器的树形入口。
 *
 * 复用 FileTreeWidget（fsbrowse:// 数据源，组件本体零改动）：目录来源 = 本地盘
 * + 设备分支挂载锚目录（R26b 定版：设备名一层根下直挂，如 C:\VM-0-16-ubuntu，
 * WinFsp 挂载读写全通）——树即文件系统，选目录与打开文件都不依赖系统资源
 * 管理器。
 *
 * 选定语义：点目录/链接行记录当前目录（FileTreeWidget onDirectoryClick 接缝）→
 * 底部路径框跟随（可手改，浏览+手输两路并存）→「选定」回填 onPick 并关闭；
 * onFileClick 透传调用方（文件打开面由入口自行接线）。
 */
import { useEffect, useState } from 'react'
import { FolderTree } from '@/assets/icons'
import { FileTreeWidget } from '@/components/schema/widgets/FileTreeWidget'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'

export interface DirectoryTreePickerModalProps {
  /** 是否打开（关闭即不渲染树，零请求） */
  open: boolean
  onClose: () => void
  /** 选定目录回调（绝对路径） */
  onPick?: (path: string) => void
  /** 文件行点击透传（打开文件面由入口接线；缺省不处理） */
  onFileClick?: (filePath: string, fileName: string) => void
  /** 模态框标题 */
  title?: string
}

export function DirectoryTreePickerModal({
  open,
  onClose,
  onPick,
  onFileClick,
  title = '选择目录',
}: DirectoryTreePickerModalProps) {
  /** 当前目录（最近点过的目录/链接行；空=未选） */
  const [currentDir, setCurrentDir] = useState('')

  // 关闭即清当前目录（下次打开不残留上次会话的路径）
  useEffect(() => {
    if (!open) setCurrentDir('')
  }, [open])

  return (
    <Dialog open={open} onOpenChange={(next) => { if (!next) onClose() }}>
      {open && (
        <DialogContent
          className="max-h-[85vh] overflow-auto sm:max-w-2xl"
          data-testid="directory-picker-modal"
        >
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2 text-base">
              <FolderTree className="h-4 w-4" />
              {title}
            </DialogTitle>
          </DialogHeader>

          <div className="max-h-[55vh] overflow-auto rounded-md border">
            <FileTreeWidget
              dataSource="fsbrowse://roots"
              nodeTitleField="name"
              showStatus={false}
              showSearch
              expandLevel={0}
              onDirectoryClick={(node: Record<string, unknown>) => {
                const path = (node.path as string) ?? ''
                if (path) setCurrentDir(path)
              }}
              onFileClick={onFileClick}
            />
          </div>

          <div className="flex items-center gap-2">
            <input
              type="text"
              data-testid="directory-picker-current"
              value={currentDir}
              onChange={(e) => setCurrentDir(e.target.value)}
              placeholder="点击目录行选择，或直接输入路径"
              className="bg-background border-input focus:bg-background w-full flex-1 rounded-md border px-3 py-2 text-sm outline-none"
            />
            <Button
              variant="outline"
              size="sm"
              data-testid="directory-picker-cancel"
              onClick={onClose}
            >
              取消
            </Button>
            <Button
              size="sm"
              data-testid="directory-picker-confirm"
              disabled={!currentDir.trim()}
              onClick={() => {
                const picked = currentDir.trim()
                if (!picked) return
                onPick?.(picked)
                onClose()
              }}
            >
              选定
            </Button>
          </div>
        </DialogContent>
      )}
    </Dialog>
  )
}
