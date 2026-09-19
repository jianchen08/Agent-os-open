/**
 * 附件预览打开服务
 *
 * 将聊天附件在工作区文件预览标签页中打开。复用 __file_editor__ Tab +
 * FilePreview/CodeEditor 渲染链路，区别在于附件走 /uploads 直链
 * （不依赖 workspaces API）。
 *
 * - 图片/PDF：靠附件直链 url 渲染（FilePreview image/pdf 分支）
 * - 纯文本/代码：经 apiClient 拉取附件 url 内容后交给 CodeEditor
 * - 二进制文档（docx/xlsx）：前端无法解析，走 binary 提示
 */

import { toast } from 'sonner'
import apiClient from '@/services/api/client'
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
 * 图片/PDF 无需读取内容；文本/代码会经 apiClient 拉取附件 URL 文本。
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
  // 文本/代码经 apiClient 取内容：认证头/重试/错误翻译统一走拦截器。
  // baseURL 强制空 = 保持 /uploads 直链的 origin 相对语义；responseType text
  // 阻止 JSON 附件被默认 transform 解析成对象（CodeEditor 内容契约是字符串）。
  // 失败 UX 由本服务自持（silent 请求标记防拦截器双吐司）；Tab 仍打开，
  // 内容留空由 CodeEditor/FilePreview 显示占位，url 兜底渲染。
  let content = ''
  if (!isMedia) {
    try {
      const resp = await apiClient.get<string>(url, {
        baseURL: '',
        responseType: 'text',
        silent: true,
      })
      content = resp.data
    } catch (e) {
      const status = (e as { response?: { status?: number } } | null)?.response?.status
      toast.error(
        status
          ? `附件 "${name}" 加载失败（HTTP ${status}）`
          : `附件 "${name}" 加载失败，请检查网络或附件是否可用`,
      )
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
