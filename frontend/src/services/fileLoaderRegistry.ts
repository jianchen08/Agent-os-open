/**
 * 文件加载器路由（docs/working/文件加载器路由设计_20260928.md）。
 *
 * 文件"能否打开"由前端加载器注册表裁决（扩展名/MIME → 查看器词汇），
 * 内核 /uploads 白名单（匿名通道安全边界）不参与决策。全部 loader 统一
 * 经鉴权内容端点取流（artifacts_file_content，键 = /uploads url basename）
 * ——前端对内核匿名通道的媒体扩展名清单零依赖。
 *
 * 词汇表哲学对齐 renderIntent：loader 是共享词汇不是组件绑定——插件
 * （波3 经 plugin.json file_loaders 声明装载）只声明"我的类型该用哪个
 * 加载器"，查看器组件全为内置，多插件天然共享。
 *
 * 解析序（确定性，与页签渲染分派一致——ext 同时决定加载器与查看器组件）：
 * 插件扩展名 → 内置扩展名 → 插件 mime → 内置 mime → null（无对应加载器卡）。
 */

import { toast } from 'sonner'
import apiClient from '@/services/api/client'
import { ARTIFACTS_ENDPOINTS } from '@/services/api/endpoints.generated'
import { registerFileEditor } from '@/stores/fileEditorRegistry'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import { UPLOADS_URL_PREFIX } from '@/utils/attachmentRefs'

/** 加载器词汇表：词汇可增长（video/table 等），组件是内置的 */
export type LoaderId = 'image' | 'pdf' | 'text'

/** 绑定声明形态（内置表与插件 file_loaders 声明共用） */
export interface FileLoaderBinding {
  loader: LoaderId
  extensions?: string[]
  mimes?: string[]
}

/** 打开目标（结构兼容 chat/types 的 Attachment） */
export interface FileOpenTarget {
  id?: string
  name: string
  url: string
  mime?: string
}

export interface ResolvedLoader {
  loader: LoaderId
  source: 'builtin' | string
}

// ── 内置表（波1）：扩展名集与工作区页签渲染分派同口径（fileEditors.ts/
//    FiveSpaceLayout：image_viewer 扩展名 / .pdf / 其余文本进 CodeEditor）──

const BUILTIN_IMAGE_EXTS = new Set(['.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.ico'])
const BUILTIN_PDF_EXTS = new Set(['.pdf'])
const BUILTIN_TEXT_EXTS = new Set([
  '.txt', '.md', '.markdown', '.log', '.csv', '.tsv',
  '.json', '.yaml', '.yml', '.toml', '.xml', '.ini', '.cfg', '.conf',
  '.properties', '.env', '.svg',
  '.py', '.js', '.jsx', '.ts', '.tsx', '.java', '.kt', '.go', '.rs',
  '.c', '.cpp', '.h', '.hpp', '.cs', '.rb', '.php', '.swift', '.dart',
  '.sh', '.bash', '.bat', '.ps1', '.sql', '.r', '.lua', '.pl',
  '.vue', '.svelte', '.scss', '.less', '.css', '.html', '.htm',
  '.graphql', '.gql', '.proto', '.zig',
  '.gitignore', '.dockerignore', '.editorconfig', '.eslintrc',
  '.prettierrc', '.dockerfile', '.makefile', '.gradle', '.cmake', '.lock', '.map',
])
const BUILTIN_MIME_RULES: ReadonlyArray<{ loader: LoaderId; matches: (mime: string) => boolean }> = [
  { loader: 'pdf', matches: (m) => m === 'application/pdf' },
  { loader: 'image', matches: (m) => m.startsWith('image/') },
  { loader: 'text', matches: (m) => m.startsWith('text/') },
  {
    loader: 'text',
    matches: (m) =>
      ['application/json', 'application/xml', 'application/javascript', 'application/x-yaml', 'application/x-sh'].includes(m),
  },
]

/** 插件声明层（波3 经 schema 装载；清空重装对齐 loadRenderIntents 语义） */
const pluginBindings: Array<{ source: string; binding: FileLoaderBinding }> = []

/** 追加插件绑定声明（坏 loader 词汇丢弃，防坏声明崩注册表——对齐 normalizeRenderIntent） */
export function registerFileLoaderBindings(bindings: FileLoaderBinding[], source: string): void {
  for (const binding of bindings) {
    const loader = (binding as { loader?: string }).loader
    if (loader !== 'image' && loader !== 'pdf' && loader !== 'text') continue
    pluginBindings.push({ source, binding: { ...binding, loader } })
  }
}

/** 清空插件声明（测试/schema 重装用） */
export function clearPluginFileLoaderBindings(): void {
  pluginBindings.length = 0
}

/** 提取扩展名（小写含点；无点/隐藏文件返回小写全名，与 fileEditors 同口径） */
function extractExt(fileName: string): string {
  const lastSlash = Math.max(fileName.lastIndexOf('/'), fileName.lastIndexOf('\\'))
  const baseName = fileName.substring(lastSlash + 1)
  const dotIndex = baseName.lastIndexOf('.')
  return (dotIndex === -1 ? baseName : baseName.substring(dotIndex)).toLowerCase()
}

function matchExt(ext: string, binding?: FileLoaderBinding): LoaderId | null {
  if (binding) {
    if (binding.extensions?.some((e) => e.toLowerCase() === ext)) return binding.loader
    return null
  }
  if (BUILTIN_IMAGE_EXTS.has(ext)) return 'image'
  if (BUILTIN_PDF_EXTS.has(ext)) return 'pdf'
  if (BUILTIN_TEXT_EXTS.has(ext)) return 'text'
  return null
}

/** 插件绑定 mime 匹配（内置 mime 兜底由 resolveLoader 内联 BUILTIN_MIME_RULES 环持有） */
function matchPluginMime(mime: string, binding: FileLoaderBinding): LoaderId | null {
  return binding.mimes?.some((m) => m.toLowerCase() === mime.toLowerCase()) ? binding.loader : null
}

/** 解析加载器：插件扩展名 → 内置扩展名 → 插件 mime → 内置 mime → null */
export function resolveLoader(fileName: string, mime?: string): ResolvedLoader | null {
  const ext = extractExt(fileName)
  for (const { source, binding } of pluginBindings) {
    const loader = matchExt(ext, binding)
    if (loader) return { loader, source }
  }
  const builtinByExt = matchExt(ext)
  if (builtinByExt) return { loader: builtinByExt, source: 'builtin' }
  if (mime) {
    for (const { source, binding } of pluginBindings) {
      const loader = matchPluginMime(mime, binding)
      if (loader) return { loader, source }
    }
    for (const rule of BUILTIN_MIME_RULES) {
      if (rule.matches(mime)) return { loader: rule.loader, source: 'builtin' }
    }
  }
  return null
}

/** /uploads url basename → 鉴权内容端点路径（url 公开契约，前端零 file_id 推导） */
function contentUrlOf(url: string): string {
  const basename = decodeURIComponent(url.split('/').pop() ?? '')
  return ARTIFACTS_ENDPOINTS.artifacts_file_content.replace(
    '{stored_filename}',
    encodeURIComponent(basename),
  )
}

function toastLoadFailure(name: string, error: unknown): void {
  const status = (error as { response?: { status?: number } } | null)?.response?.status
  toast.error(
    status
      ? `附件 "${name}" 加载失败（HTTP ${status}）`
      : `附件 "${name}" 加载失败，请检查网络或附件是否可用`,
  )
}

/**
 * 文件打开统一入口：路由到加载器 → 工作区页签。
 *
 * - 命中：鉴权端点取流（text→文本 / image,pdf→blob objectURL）后开页签；
 *   取流失败 → 报错通知，不开页签（文件找不到的用户契约落点）；
 * - 未命中：「无对应的加载器」卡页签（下载动作按需取流）；
 * - 已打开：激活现有页签（按 id/url 去重）。
 */
export async function openFileWithLoader(target: FileOpenTarget): Promise<void> {
  const layoutStore = useLayoutModeStore.getState()
  const tabId = `attach-${target.id || target.url}`

  const existing = layoutStore.workspaceTabs.find((t) => t.id === tabId)
  if (existing) {
    layoutStore.setActiveTab(tabId)
    return
  }

  const resolved = resolveLoader(target.name, target.mime)
  if (!resolved) {
    registerFileEditor(tabId, {
      filePath: target.name,
      fileName: target.name,
      content: '',
      containerTaskId: '',
      url: contentUrlOf(target.url),
      viewerOverride: 'none',
    })
    layoutStore.addWorkspaceTab({
      id: tabId,
      title: target.name,
      icon: '📄',
      moduleId: '__file_editor__',
      isActive: true,
      isPinned: false,
    })
    return
  }

  const asText = resolved.loader === 'text'
  let data: { content: string; url?: string; blobUrl?: string }
  try {
    const resp = await apiClient.get<string | Blob>(contentUrlOf(target.url), {
      baseURL: '',
      responseType: asText ? 'text' : 'blob',
      silent: true,
    })
    if (asText) {
      data = { content: String(resp.data ?? '') }
    } else {
      const blobUrl = URL.createObjectURL(resp.data as Blob)
      data = { content: '', url: blobUrl, blobUrl }
    }
  } catch (error) {
    toastLoadFailure(target.name, error)
    return
  }

  registerFileEditor(tabId, {
    filePath: target.name,
    fileName: target.name,
    containerTaskId: '',
    ...data,
  })
  layoutStore.addWorkspaceTab({
    id: tabId,
    title: target.name,
    icon: '📎',
    moduleId: '__file_editor__',
    isActive: true,
    isPinned: false,
  })
}

/**
 * markdown 链接拦截工厂（LobeChatMarkdown onLinkClick 消费——组件零加载器知识，
 * /uploads 策略单一来源在此）：
 *  - /uploads 附件直链 → 走加载器打开（文件名取链接文本，即原始文件名），返回 true
 *  - 其余链接 → 放行默认行为，返回 false
 */
export function markdownLinkInterceptor(): (link: { href: string; text: string }) => boolean {
  return ({ href, text }) => {
    if (!href.startsWith(UPLOADS_URL_PREFIX)) return false
    const name = text.trim() || decodeURIComponent(href.split('/').pop() ?? '') || '附件'
    void openFileWithLoader({ name, url: href })
    return true
  }
}
