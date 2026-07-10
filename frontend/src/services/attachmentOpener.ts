/**
 * 附件预览打开服务
 *
 * 将聊天附件在工作区文件预览标签页中打开。复用 __file_editor__ Tab +
 * FilePreview/CodeEditor 渲染链路，区别在于附件走 /uploads 直链
 * （不依赖 workspaces API）。
 *
 * - 图片/PDF：靠附件直链 url 渲染（FilePreview image/pdf 分支）
 * - 纯文本/代码：fetch 附件 url 拿内容后交给 CodeEditor
 * - 二进制文档（docx/xlsx）：前端无法解析，走 binary 提示
 */

import { registerFileEditor } from '@/stores/fileEditorRegistry'
import { useLayoutModeStore } from '@/stores/layoutModeStore'

/** 可通过 URL 直接渲染的媒体扩展名（图片 + PDF） */
const MEDIA_EXTENSIONS = /\.(png|jpe?g|gif|webp|bmp|ico|pdf)$/i

/** 打开附件所需的最小信息 */
export interface AttachmentOpenTarget {
  /** 附件 ID（用于 Tab 去重，缺失时回退到 url） */
  id?: string
  /** 文件名（含扩展名，用于扩展名判断和展示） */
  name: string
  /** 附件可访问 URL（如 /uploads/xxx.pdf） */
  url: string
}

/**
 * 在工作区文件预览标签页打开附件。
 *
 * 已打开的同附件会直接激活现有 Tab（按 id/url 去重）。
 * 图片/PDF 无需读取内容；文本/代码会 fetch 附件 URL 获取文本。
 *
 * @param target - 附件目标信息
 */
export async function openAttachment(target: AttachmentOpenTarget): Promise<void> {
  const { id, name, url } = target
  const tabId = `attach-${id || url}`
  const layoutStore = useLayoutModeStore.getState()

  // 去重：已存在则直接激活
  const existing = layoutStore.workspaceTabs.find((t) => t.id === tabId)
  if (existing) {
    layoutStore.setActiveTab(tabId)
    return
  }

  const isMedia = MEDIA_EXTENSIONS.test(name)
  // 图片/PDF 靠 url 渲染；文本/代码需 fetch 内容
  let content = ''
  if (!isMedia) {
    try {
      const resp = await fetch(url)
      if (resp.ok) {
        content = await resp.text()
      }
    } catch {
      // 二进制或网络失败：留空，CodeEditor/FilePreview 会显示占位
    }
  }

  registerFileEditor(tabId, {
    filePath: name,
    fileName: name,
    content,
    url,
    containerTaskId: '', // 附件无工作区容器，靠 url 直链渲染
  })

  layoutStore.addWorkspaceTab({
    id: tabId,
    title: name,
    icon: '📎',
    moduleId: '__file_editor__',
    isActive: true,
    isPinned: false,
  })
}
