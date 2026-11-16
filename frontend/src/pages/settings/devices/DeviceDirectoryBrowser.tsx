/**
 * 设备目录浏览（设备页区块）：多设备目录树展开 + 打开文件。
 *
 * 复用 FileTreeWidget（fsbrowse:// 数据源，组件本体零改动）：目录来源 = 本地盘
 * + 设备分支挂载锚目录（R26b 定版：设备名一层根下直挂，WinFsp 挂载读写全通，
 * NAS 体验）。文件行点击 = open-file 端点（宿主默认程序打开，与工作空间
 * open-file 同端点语义）；失败 toast 不静默。
 *
 * 折叠默认零请求；展开即拉 fsbrowse 根层。
 */
import { useState } from 'react'
import { toast } from 'sonner'
import { FolderTree } from '@/assets/icons'
import { FileTreeWidget } from '@/components/schema/widgets/FileTreeWidget'
import apiClient from '@/services/api/client'
import { WORKSPACE_SERVICE_ENDPOINTS } from '@/services/api/endpoints.generated'

/** 设备目录浏览区块（设备页内嵌，无自有标题栏） */
export function DeviceDirectoryBrowser() {
  const [open, setOpen] = useState(false)

  /** 文件行点击 = 宿主默认程序打开（open-file 端点，任意路径） */
  const handleFileClick = async (filePath: string) => {
    try {
      const resp = await apiClient.post(WORKSPACE_SERVICE_ENDPOINTS.workspaces_open_file, {
        file_path: filePath,
      })
      if (resp.data?.success) {
        toast.success(resp.data.message ?? `已打开文件: ${filePath}`)
      } else {
        toast.error(resp.data?.message ?? `打开文件失败: ${filePath}`)
      }
    } catch (err) {
      toast.error(`打开文件失败：${err instanceof Error ? err.message : String(err)}`)
    }
  }

  return (
    <div>
      <button
        type="button"
        data-testid="device-tree-toggle"
        onClick={() => setOpen((prev) => !prev)}
        className="border-border hover:bg-muted/30 flex items-center gap-1.5 rounded-lg border px-2.5 py-1 text-xs"
      >
        <FolderTree className="h-3.5 w-3.5" />
        {open ? '收起目录' : '浏览设备目录'}
      </button>
      {open && (
        <div className="border-border mt-3 max-h-[50vh] overflow-auto rounded-lg border p-1">
          <FileTreeWidget
            dataSource="fsbrowse://roots"
            nodeTitleField="name"
            showStatus={false}
            showSearch
            expandLevel={0}
            onFileClick={(filePath: string, _fileName: string) => {
              void handleFileClick(filePath)
            }}
          />
        </div>
      )}
    </div>
  )
}
