/**
 * 文档查看器（PDF / Word / PPT，批 5）
 *
 * 三格式统一外壳：只读渲染（文档本体不可编辑，方案 §3.6.5）+ 批注侧栏。
 * 渲染件全部接入成熟包，不自研解析：
 *
 * | 格式 | 渲染件 | 说明 |
 * |---|---|---|
 * | `.pdf` | **pdfjs-dist**（Apache-2.0） | 逐页取文本层（供选区锚定）；渲染失败降级浏览器内置 iframe |
 * | `.docx` | **docx-preview**（Apache-2.0） | `renderAsync` 产 DOM（版式保真优于 mammoth） |
 * | `.pptx` | **pptx-preview**（ISC，有条件） | `init` + `preview`；失败降级「页级占位」（方案 §3.6.2 兜底口径） |
 *
 * 批注层自研（无成熟件，方案 §3.6.3）：锚定逻辑在 `docAnnotation.ts`（纯函数、
 * 可单测），本组件只负责接线与展示。
 *
 * 数据落点如实交代：批注走既有端点（list/create）+ 插件内存存储，
 * **进程重启即丢** → 侧栏固定显示「本次会话内批注」，不假装已持久化。
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { AlertCircle, Loader2, MessageSquare } from '@/assets/icons'
import {
  annotationsToMarkdown,
  createAnnotation,
  describeTarget,
  listAnnotations,
} from '@/services/api/annotations'
import { annotationRects, detectDocumentFormat, selectionToTarget } from './docAnnotation'
import type { DocumentSelectionTarget, HighlightRect } from './docAnnotation'
import type { Annotation } from '@/types/artifact'
import type { FC } from 'react'

/** 取文档二进制（url 优先，content 兜底；两者都缺返回 undefined） */
async function loadBytes(url?: string, content?: string): Promise<Uint8Array | undefined> {
  if (url) {
    const response = await fetch(url)
    if (!response.ok) throw new Error(`取流失败: ${response.status}`)
    return new Uint8Array(await response.arrayBuffer())
  }
  if (typeof content === 'string' && content !== '') {
    return new TextEncoder().encode(content)
  }
  return undefined
}

export interface DocumentViewerProps {
  /** 文件路径（页签编辑器数据同源） */
  filePath: string
  /** 文件内容（docx/pptx 需要字节；pdf 用 url 即可） */
  content?: string
  /** 取流 URL（pdf.js 数据源与 iframe 兜底） */
  url?: string
  /** 批注归属的产物 ID（artifacts 批注按 artifact_id 关联） */
  artifactId: string
}

/** 文档查看器（三格式路由 + 批注层） */
export const DocumentViewer: FC<DocumentViewerProps> = ({ filePath, content, url, artifactId }) => {
  const format = detectDocumentFormat(filePath)
  const containerRef = useRef<HTMLDivElement | null>(null)
  const [annotations, setAnnotations] = useState<Annotation[]>([])
  const [pending, setPending] = useState<DocumentSelectionTarget | null>(null)
  const [draft, setDraft] = useState('')
  const [saving, setSaving] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)
  const [renderRects, setRenderRects] = useState<Array<{ id: string; rects: HighlightRect[] }>>([])

  /** 载入既有批注（失败不阻塞阅读，给可见提示） */
  useEffect(() => {
    let cancelled = false
    if (!artifactId) return
    void (async () => {
      try {
        const items = await listAnnotations(artifactId)
        if (!cancelled) setAnnotations(items)
      } catch {
        if (!cancelled) setNotice('批注加载失败（可继续阅读，稍后重试）')
      }
    })()
    return () => {
      cancelled = true
    }
  }, [artifactId])

  /** 文本选区 → 待提交批注锚点（鼠标抬起时算） */
  const handleMouseUp = useCallback(() => {
    const root = containerRef.current
    if (!root) return
    const target = selectionToTarget(root, root.ownerDocument.getSelection())
    if (target) {
      setPending(target)
      setDraft('')
    }
  }, [])

  /** 提交批注 */
  const submit = useCallback(async () => {
    if (!pending || draft.trim() === '') return
    setSaving(true)
    try {
      const created = await createAnnotation(artifactId, {
        targetType: 'text_selection',
        targetData: {
          startOffset: pending.startOffset,
          endOffset: pending.endOffset,
          selectedText: pending.selectedText,
        },
        content: draft.trim(),
        authorType: 'user',
      })
      setAnnotations((prev) => [...prev, created])
      setPending(null)
      setDraft('')
    } catch {
      setNotice('批注保存失败（当前为会话内存储，请重试）')
    } finally {
      setSaving(false)
    }
  }, [artifactId, draft, pending])

  /** 批注变化后重算高亮矩形（拿不到矩形就不高亮，但侧栏条目照常显示） */
  useEffect(() => {
    const root = containerRef.current
    if (!root) {
      setRenderRects([])
      return
    }
    setRenderRects(
      annotations
        .filter((a) => a.targetType === 'text_selection')
        .map((a) => {
          const data = a.targetData as { startOffset?: unknown; endOffset?: unknown }
          const start = typeof data.startOffset === 'number' ? data.startOffset : -1
          const end = typeof data.endOffset === 'number' ? data.endOffset : -1
          if (start < 0 || end <= start) return { id: a.id, rects: [] }
          return { id: a.id, rects: annotationRects(root, start, end) }
        }),
    )
  }, [annotations, format])

  /** 导出批注为 Markdown（前端生成：后端无导出端点） */
  const exportMarkdown = useCallback(() => {
    const markdown = annotationsToMarkdown(annotations, { documentName: filePath, artifactId })
    const blob = new Blob([markdown], { type: 'text/markdown;charset=utf-8' })
    const link = document.createElement('a')
    link.href = URL.createObjectURL(blob)
    link.download = `${filePath.split(/[/\\]/).pop() ?? 'document'}-annotations.md`
    link.click()
    URL.revokeObjectURL(link.href)
  }, [annotations, artifactId, filePath])

  if (!format) {
    return (
      <div className="text-muted-foreground flex h-full items-center justify-center text-sm">
        不支持的文档格式（仅支持 .pdf / .docx / .pptx）
      </div>
    )
  }

  return (
    <div className="flex h-full min-h-0" data-testid="document-viewer" data-format={format}>
      {/* 文档渲染区（只读；批注高亮叠层） */}
      <div className="bg-muted/10 relative min-h-0 flex-1 overflow-auto">
        <div ref={containerRef} onMouseUp={handleMouseUp} className="relative min-h-full">
          {format === 'pdf' && <PdfBody url={url} content={content} onError={setNotice} />}
          {format === 'docx' && <DocxBody url={url} content={content} onError={setNotice} />}
          {format === 'pptx' && <PptxBody url={url} content={content} onError={setNotice} />}

          <div className="pointer-events-none absolute inset-0" data-testid="doc-highlights">
            {renderRects.flatMap(({ id, rects }) =>
              rects.map((rect, index) => (
                <span
                  key={`${id}-${index}`}
                  className="bg-status-waiting/30 absolute rounded-sm"
                  // eslint-disable-next-line no-restricted-syntax -- 高亮矩形是按文本 Range 实算的百分比坐标，静态类无法表达
                  style={{
                    left: `${rect.left * 100}%`,
                    top: `${rect.top * 100}%`,
                    width: `${rect.width * 100}%`,
                    height: `${rect.height * 100}%`,
                  }}
                />
              )),
            )}
          </div>
        </div>

        {/* 选区浮层：选中文字后提出「加批注」 */}
        {pending && (
          <div
            className="border-border bg-background absolute right-3 bottom-3 left-3 rounded border p-2 shadow-lg"
            data-testid="doc-annotation-composer"
          >
            <div className="text-muted-foreground mb-1 truncate text-[11px]">
              选中：{pending.selectedText.slice(0, 60)}
              {pending.selectedText.length > 60 ? '…' : ''}（{pending.startOffset}-
              {pending.endOffset}）
            </div>
            <textarea
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              placeholder="写批注…（文档只读，批注与文档分离存储）"
              className="border-border bg-background h-16 w-full resize-none rounded border px-2 py-1 text-xs"
              data-testid="doc-annotation-input"
            />
            <div className="mt-1 flex items-center gap-2">
              <button
                type="button"
                onClick={() => void submit()}
                disabled={saving || draft.trim() === ''}
                className="bg-primary text-primary-foreground rounded px-2 py-0.5 text-[11px] disabled:opacity-50"
                data-testid="doc-annotation-submit"
              >
                {saving ? '保存中…' : '添加批注'}
              </button>
              <button
                type="button"
                onClick={() => setPending(null)}
                className="text-muted-foreground text-[11px] underline"
              >
                取消
              </button>
            </div>
          </div>
        )}
      </div>

      {/* 批注侧栏：会话内提示 + 列表 + 导出 */}
      <aside
        className="border-border flex w-[280px] flex-shrink-0 flex-col border-l"
        data-testid="doc-annotation-sidebar"
      >
        <div className="border-border flex flex-shrink-0 items-center gap-1.5 border-b px-2 py-1.5">
          <MessageSquare className="text-muted-foreground h-icon-sm w-icon-sm" />
          <span className="text-foreground text-xs font-medium">批注 {annotations.length}</span>
          <button
            type="button"
            onClick={exportMarkdown}
            disabled={annotations.length === 0}
            className="text-muted-foreground hover:text-foreground ml-auto text-[11px] underline disabled:opacity-40"
            data-testid="doc-annotation-export"
          >
            导出
          </button>
        </div>

        {/* 诚实标注：内存态，重启丢失（不假装持久化） */}
        <div className="text-status-waiting bg-status-waiting/10 flex-shrink-0 px-2 py-1 text-[10px]">
          本次会话内批注：批注存于插件内存，内核重启后丢失；文档本体只读不改写
        </div>

        {notice && (
          <div className="text-status-waiting flex flex-shrink-0 items-start gap-1 px-2 py-1 text-[10px]">
            <AlertCircle className="h-icon-xs w-icon-xs flex-shrink-0" />
            <span>{notice}</span>
          </div>
        )}

        <div className="min-h-0 flex-1 overflow-auto" data-testid="doc-annotation-list">
          {annotations.length === 0 ? (
            <div className="text-muted-foreground p-3 text-center text-[11px]">
              还没有批注：选中文字即可添加
            </div>
          ) : (
            annotations.map((annotation) => (
              <div
                key={annotation.id}
                className="border-border/60 border-b px-2 py-1.5"
                data-testid="doc-annotation-item"
              >
                <div className="text-muted-foreground/70 flex items-center gap-1 text-[10px]">
                  <span>{annotation.authorType === 'agent' ? '机器' : '用户'}</span>
                  {annotation.status === 'resolved' && <span>· 已解决</span>}
                </div>
                <div
                  className="text-muted-foreground truncate text-[10px]"
                  title={describeTarget(annotation)}
                >
                  {describeTarget(annotation)}
                </div>
                <div className="text-foreground mt-0.5 text-xs whitespace-pre-wrap">
                  {annotation.content}
                </div>
              </div>
            ))
          )}
        </div>
      </aside>
    </div>
  )
}

/** PDF 正文：pdf.js 取逐页文本层（可选中 → 可批注）；失败降级浏览器内置 iframe */
const PdfBody: FC<{ url?: string; content?: string; onError: (msg: string) => void }> = ({
  url,
  content,
  onError,
}) => {
  const [pages, setPages] = useState<string[] | null>(null)
  const [failed, setFailed] = useState(false)

  useEffect(() => {
    let cancelled = false
    void (async () => {
      try {
        const bytes = await loadBytes(url, content)
        if (!bytes) throw new Error('缺少文档数据')
        const pdfjs = await import('pdfjs-dist')
        const workerSrc = await import('pdfjs-dist/build/pdf.worker.min.mjs?url')
        pdfjs.GlobalWorkerOptions.workerSrc = workerSrc.default as string
        const doc = await pdfjs.getDocument({ data: bytes }).promise
        const texts: string[] = []
        for (let i = 1; i <= doc.numPages; i += 1) {
          const page = await doc.getPage(i)
          const textContent = await page.getTextContent()
          texts.push(
            textContent.items
              .map((item) => ('str' in item ? item.str : ''))
              .join(' ')
              .replace(/\s+/g, ' ')
              .trim(),
          )
        }
        if (!cancelled) setPages(texts)
      } catch (error) {
        if (!cancelled) {
          setFailed(true)
          onError(`PDF 渲染失败，已降级为浏览器内置预览：${(error as Error).message}`)
        }
      }
    })()
    return () => {
      cancelled = true
    }
  }, [content, onError, url])

  if (failed) {
    return url ? (
      <iframe src={url} className="h-full w-full border-0" title="PDF 预览（内置）" />
    ) : (
      <div className="text-muted-foreground flex h-full items-center justify-center text-sm">
        PDF 无法预览（缺少取流地址）
      </div>
    )
  }
  if (!pages) {
    return (
      <div className="text-muted-foreground flex h-full items-center justify-center gap-2 text-xs">
        <Loader2 className="h-icon-sm w-icon-sm animate-spin" />
        PDF 解析中…
      </div>
    )
  }
  return (
    <div className="space-y-3 p-3" data-testid="pdf-text-layer">
      {pages.map((text, index) => (
        <section key={index} className="border-border/60 rounded border p-2">
          <div className="text-muted-foreground/60 mb-1 text-[10px]">第 {index + 1} 页</div>
          <div className="text-foreground text-xs leading-relaxed whitespace-pre-wrap select-text">
            {text || '（本页无可选中文本）'}
          </div>
        </section>
      ))}
    </div>
  )
}

/** Word 正文：docx-preview 渲染 DOM（版式保真） */
const DocxBody: FC<{ url?: string; content?: string; onError: (msg: string) => void }> = ({
  url,
  content,
  onError,
}) => {
  const hostRef = useRef<HTMLDivElement | null>(null)
  const [failed, setFailed] = useState(false)

  useEffect(() => {
    let cancelled = false
    const host = hostRef.current
    if (!host) return
    void (async () => {
      try {
        const bytes = await loadBytes(url, content)
        if (!bytes) throw new Error('缺少文档数据')
        const { renderAsync } = await import('docx-preview')
        host.innerHTML = ''
        await renderAsync(bytes, host, undefined, { inWrapper: true, ignoreWidth: true })
        if (cancelled) host.innerHTML = ''
      } catch (error) {
        if (!cancelled) {
          setFailed(true)
          onError(`Word 渲染失败：${(error as Error).message}`)
        }
      }
    })()
    return () => {
      cancelled = true
    }
  }, [content, onError, url])

  if (failed) {
    return (
      <div className="text-muted-foreground flex h-full items-center justify-center p-4 text-center text-sm">
        Word 文档渲染失败（可下载后用本机 Word 打开）
      </div>
    )
  }
  return <div ref={hostRef} className="docx-host p-3 text-xs select-text" data-testid="docx-host" />
}

/** PPT 正文：pptx-preview 预览；失败降级「页级占位」（方案 §3.6.2 兜底） */
const PptxBody: FC<{ url?: string; content?: string; onError: (msg: string) => void }> = ({
  url,
  content,
  onError,
}) => {
  const hostRef = useRef<HTMLDivElement | null>(null)
  const [failed, setFailed] = useState(false)

  useEffect(() => {
    let cancelled = false
    const host = hostRef.current
    if (!host) return
    void (async () => {
      try {
        const bytes = await loadBytes(url, content)
        if (!bytes) throw new Error('缺少文档数据')
        const mod = await import('pptx-preview')
        const init = mod.init
        if (typeof init !== 'function') throw new Error('pptx-preview 无 init 导出')
        host.innerHTML = ''
        const instance = init(host, { width: 960, height: 540 })
        const buffer = bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength)
        await instance.preview(buffer as ArrayBuffer)
      } catch (error) {
        if (!cancelled) {
          setFailed(true)
          onError(`PPT 渲染失败，已降级为页级占位：${(error as Error).message}`)
        }
      }
    })()
    return () => {
      cancelled = true
    }
  }, [content, onError, url])

  if (failed) {
    return (
      <div className="p-4 text-xs" data-testid="pptx-placeholder">
        <div className="text-muted-foreground mb-2">
          PPT 页面内容暂无法内嵌渲染（渲染件不可用）。仍可在右侧写批注，批注与文档分离存储。
        </div>
        <div className="border-border/60 text-muted-foreground rounded border p-3">
          提示：可在本机 PowerPoint 打开原文件；批注可经侧栏「导出」带走。
        </div>
      </div>
    )
  }
  return <div ref={hostRef} className="p-3 select-text" data-testid="pptx-host" />
}
