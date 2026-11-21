/**
 * 文件查看器声明注册表（声明驱动，ADR 2026-10-24-file-viewer-declarative）
 *
 * 三层路由：插件 file_viewers 声明（plugin.json contributes.file_viewers，经
 * /api/v1/schema 的 plugin_contributes 通道原样透传，内核零解释）> 内置声明表
 * > 默认文本编辑器兜底。
 *
 * 插件查看器渲染走 webview 沙箱（render.widget='webview'，props 形态同
 * WebviewWidgetProps：pluginId/htmlPath/widgetId...）；文件坐标（path/
 * container_task_id）经 widgetEventStore 下行 'file.open' 事件注入页面——与
 * open_page 的 locate 定位同款先例（立即推 + 延迟重推覆盖页面加载窗口）。
 * 内置查看器（文本/图片/PDF/HTML）渲染组件路径不变：本刀只声明化**路由判定**，
 * 内置组件 widget 化留后续（见 ADR 影响节）。
 *
 * 坏声明 fail-closed：id/label/render.widget 非字符串、extensions 非 . 前缀
 * 数组等结构违规整条丢弃（console.warn 可观察），防坏声明崩注册表——对齐
 * registerFileLoaderBindings / normalizeRenderIntent 的装载哲学。
 */

import type { AdaptedWidgetEvent } from '@/services/websocket/MessageAdapter'

/** 查看器渲染声明：widget 为前端 widgetRegistry 组件名（插件一律 'webview'） */
export interface FileViewerRender {
  widget: string
  /** 组件 props 原样透传（webview 形态：pluginId/htmlPath/widgetId/csp...） */
  props?: Record<string, unknown>
}

/** 文件查看器声明（内置表与插件 contributes.file_viewers 共用形态） */
export interface FileViewerDeclaration {
  id: string
  label: string
  /** 扩展名绑定（小写、含点号，如 '.pptx'） */
  extensions: string[]
  /** MIME 绑定（可选，文件元数据带 mime 时优先于扩展名外的兜底信号） */
  mimes?: string[]
  render: FileViewerRender
}

export interface ResolvedFileViewer {
  viewer: FileViewerDeclaration
  /** 声明来源：'builtin' 或插件 id */
  source: 'builtin' | string
}

/** 内置声明表：显式三条 + 默认文本兜底（行为等价旧 fileEditors.fileTypeMap——
 * 其 70+ 显式文本扩展名最终都落 text_editor，未列出项同样落默认） */
const BUILTIN_VIEWERS: FileViewerDeclaration[] = [
  {
    id: 'image_viewer',
    label: '图片查看器',
    // .svg 走同一图片查看器：浏览器原生可渲染（矢量按 viewBox 缩放），
    // 不必单列一个查看器类型（与 FilePreview.IMAGE_EXTENSIONS 同集合）
    extensions: ['.png', '.jpg', '.jpeg', '.gif', '.webp', '.ico', '.bmp', '.svg'],
    render: { widget: 'image_preview' },
  },
  {
    id: 'markdown_viewer',
    label: 'Markdown 预览',
    // .md 之前落默认文本编辑器（只显示源码）；渲染态预览单列，语法高亮源码
    // 仍可从查看器内切换（FilePreview markdown 分支同时给「原文」入口）
    extensions: ['.md', '.markdown'],
    render: { widget: 'markdown_preview' },
  },
  {
    id: 'media_viewer',
    label: '音视频播放',
    // 浏览器原生 <video>/<audio>；取流走 workspace file-content 的原始字节面
    // （FilePreview 拼同一 URL，不走文本解码）
    extensions: ['.mp4', '.webm', '.mov', '.m4v', '.mp3', '.wav', '.ogg', '.m4a', '.flac'],
    render: { widget: 'media_preview' },
  },
  {
    id: 'binary_viewer',
    label: '二进制文件提示',
    // 冷门/不可渲染类型（压缩包、可执行、字体…）：明确提示「不支持预览」+
    // 下载 / 外部打开，不塞进文本编辑器吐出乱码（本轮之前 .zip 落 code_editor
    // 会显示一屏替换字符）
    extensions: [
      '.zip', '.rar', '.7z', '.gz', '.tar', '.bz2', '.xz',
      '.exe', '.msi', '.dll', '.so', '.dylib', '.bin', '.iso', '.dmg', '.apk',
      '.woff', '.woff2', '.ttf', '.otf', '.eot',
      '.db', '.sqlite', '.sqlite3', '.pyc', '.class', '.jar',
    ],
    render: { widget: 'binary_notice' },
  },
  {
    id: 'pdf_viewer',
    label: 'PDF 预览',
    extensions: ['.pdf'],
    render: { widget: 'pdf_preview' },
  },
  {
    id: 'html_preview',
    label: 'HTML 预览',
    extensions: ['.html', '.htm'],
    render: { widget: 'html_preview' },
  },
  // 文档查看器（任务现场批 5）：PDF/Word/PPT 走统一「文档查看器」渲染件
  // （pdfjs-dist / docx-preview / pptx-preview 接入 + 批注层），渲染件本体在
  // components/workspace/docViewer/DocumentViewer.tsx，装配层按 widget 名分派。
  // 声明化保留：插件后续可声明同名扩展覆盖（路由优先级 插件 > 内置）。
  {
    id: 'document_viewer',
    label: '文档查看器（可批注）',
    extensions: ['.pdf', '.docx', '.pptx'],
    render: { widget: 'document_viewer' },
  },
  // Excel 查看器（选型接入，真值 = docs/working/971600b7ff1e_查看器选型与双链方案.md
  // §1.2 Excel 节）：解析 @e965/xlsx（Apache-2.0，SheetJS 社区镜像——官方 npm
  // `xlsx` 停更 4 年不采用）+ FilePreview 自绘只读表格（前 N 行）。.xlsb 未声明
  // （BIFF 二进制变体 @e965/xlsx 支持度弱，宁缺勿滥落默认文本编辑器）。
  {
    id: 'excel_viewer',
    label: 'Excel 预览',
    extensions: ['.xlsx', '.xlsm', '.xls'],
    render: { widget: 'excel_preview' },
  },
]

const DEFAULT_VIEWER: FileViewerDeclaration = {
  id: 'text_editor',
  label: '文本编辑器',
  extensions: [],
  render: { widget: 'code_editor' },
}

/** 插件声明存储：条目按声明顺序，路由时先于内置表 */
const pluginViewers: Array<{ source: string; viewer: FileViewerDeclaration }> = []

/** 声明结构校验（fail-closed：违规返回 null，调用方丢弃整条） */
function normalizeDeclaration(
  raw: unknown,
): FileViewerDeclaration | null {
  if (typeof raw !== 'object' || raw === null) return null
  const r = raw as Record<string, unknown>
  if (typeof r.id !== 'string' || r.id === '') return null
  if (typeof r.label !== 'string' || r.label === '') return null
  const render = r.render
  if (typeof render !== 'object' || render === null) return null
  const widget = (render as Record<string, unknown>).widget
  if (typeof widget !== 'string' || widget === '') return null
  const props = (render as Record<string, unknown>).props
  if (props !== undefined && (typeof props !== 'object' || props === null)) return null
  if (!Array.isArray(r.extensions)) return null
  const extensions: string[] = []
  for (const ext of r.extensions) {
    if (typeof ext !== 'string' || !ext.startsWith('.')) return null
    extensions.push(ext.toLowerCase())
  }
  let mimes: string[] | undefined
  if (r.mimes !== undefined) {
    if (!Array.isArray(r.mimes)) return null
    mimes = []
    for (const m of r.mimes) {
      if (typeof m !== 'string' || m === '') return null
      mimes.push(m.toLowerCase())
    }
  }
  return {
    id: r.id,
    label: r.label,
    extensions,
    ...(mimes ? { mimes } : {}),
    render: { widget, ...(props ? { props: props as Record<string, unknown> } : {}) },
  }
}

/**
 * 从 plugin_contributes 装载插件查看器声明（schema 刷新全量重放：先清后装）。
 * 坏条目丢弃并 warn（不中断其余装载）。
 */
export function loadFileViewers(
  pluginContributes: Array<{ plugin_id?: string; contributes?: Record<string, unknown> }>,
): void {
  pluginViewers.length = 0
  for (const entry of pluginContributes) {
    const pluginId = entry.plugin_id
    if (typeof pluginId !== 'string' || pluginId === '') continue
    const raws = entry.contributes?.file_viewers
    if (!Array.isArray(raws)) continue
    for (const raw of raws) {
      const viewer = normalizeDeclaration(raw)
      if (viewer === null) {
        console.warn(
          `[fileViewerRegistry] 插件 ${pluginId} 的 file_viewers 声明结构违规，已丢弃:`,
          raw,
        )
        continue
      }
      pluginViewers.push({ source: pluginId, viewer })
    }
  }
}

/** 提取扩展名（小写含点号；无扩展名取整名小写——与 fileEditors 同口径） */
function extractExtension(fileName: string): string {
  const lastSlash = Math.max(fileName.lastIndexOf('/'), fileName.lastIndexOf('\\'))
  const baseName = fileName.substring(lastSlash + 1)
  if (baseName.startsWith('.') && baseName.lastIndexOf('.') === 0) {
    return baseName.toLowerCase()
  }
  const dotIndex = baseName.lastIndexOf('.')
  if (dotIndex === -1) return baseName.toLowerCase()
  return baseName.substring(dotIndex).toLowerCase()
}

/**
 * 文件 → 查看器路由：插件声明优先于内置，未命中落默认文本编辑器。
 * @param fileName 文件名/路径
 * @param mime 可选 MIME（有 mime 时插件 mime 绑定也参与匹配，扩展名仍优先）
 */
export function resolveFileViewer(
  fileName: string,
  mime?: string,
): ResolvedFileViewer {
  const ext = extractExtension(fileName)
  for (const { source, viewer } of pluginViewers) {
    if (viewer.extensions.includes(ext)) return { viewer, source }
  }
  if (mime) {
    const lowerMime = mime.toLowerCase()
    for (const { source, viewer } of pluginViewers) {
      if (viewer.mimes?.includes(lowerMime)) return { viewer, source }
    }
  }
  for (const viewer of BUILTIN_VIEWERS) {
    if (viewer.extensions.includes(ext)) return { viewer, source: 'builtin' }
  }
  return { viewer: DEFAULT_VIEWER, source: 'builtin' }
}

/** 是否插件声明查看器（webview 渲染路径判定） */
export function isPluginViewer(resolved: ResolvedFileViewer): boolean {
  return resolved.source !== 'builtin'
}

/**
 * 打开插件查看器后向页面下发文件坐标（'file.open' 事件）。
 * 立即推 + 1500ms 重推（页面可能仍在加载，重复投递幂等）——与
 * registerGlobalOpenPanelCallback 的 locate 定位同款。
 */
export function dispatchFileOpenEvent(
  dispatch: (ev: AdaptedWidgetEvent) => void,
  widgetId: string,
  filePath: string,
  containerTaskId: string | undefined,
): void {
  const push = (): void =>
    dispatch({
      widget_id: widgetId,
      event: 'file.open',
      data: { path: filePath, container_task_id: containerTaskId },
    })
  push()
  window.setTimeout(push, 1500)
}
