/** @feature FP-0.2.四 前端Schema（文档批注） @vision V6 可即用 @ci frontend-test */
/**
 * DocumentViewer 组件测试（任务现场批 5）。
 *
 * 渲染件（pdfjs-dist / docx-preview / pptx-preview）是外部重依赖，jsdom 无
 * worker/布局，全部 mock 成可控形态；断言可观察输出：
 * - 不支持格式的空态；
 * - PDF 成功（逐页文本层）/ 失败（有 url → iframe 降级；无 url → 显式失败态）/
 *   数据缺失（无 url 无 content）；
 * - DOCX 成功渲染宿主 / 失败提示；
 * - PPTX 成功渲染宿主 / 失败 → 页级占位（方案 §3.6.2 兜底）；
 * - 批注链路：列表加载与失败提示、选中→浮层→提交→新增条目、导出按钮触发
 *   Blob 下载。
 */
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { DocumentViewer } from '@/components/workspace/docViewer/DocumentViewer'
import type * as AnnotationsModule from '@/services/api/annotations'
import type { Annotation } from '@/types/artifact'

// ── 渲染件 mock（外部依赖；jsdom 无 worker/版式）──
const getDocumentMock = vi.fn()
vi.mock('pdfjs-dist', () => ({
  GlobalWorkerOptions: { workerSrc: '' },
  getDocument: (...args: unknown[]) => getDocumentMock(...args),
}))
vi.mock('pdfjs-dist/build/pdf.worker.min.mjs?url', () => ({ default: 'worker-src' }))

const renderAsyncMock = vi.fn()
vi.mock('docx-preview', () => ({ renderAsync: (...args: unknown[]) => renderAsyncMock(...args) }))

const pptxInitMock = vi.fn()
vi.mock('pptx-preview', () => ({ init: (...args: unknown[]) => pptxInitMock(...args) }))

// ── 批注 API mock（网络层；annotations.ts 自有单测）──
const listAnnotationsMock = vi.fn()
const createAnnotationMock = vi.fn()
vi.mock('@/services/api/annotations', async (importOriginal) => {
  const actual = await importOriginal<AnnotationsModule>()
  return {
    ...actual,
    listAnnotations: (...args: unknown[]) => listAnnotationsMock(...args),
    createAnnotation: (...args: unknown[]) => createAnnotationMock(...args),
  }
})


function makeAnnotation(overrides: Partial<Annotation> = {}): Annotation {
  return {
    id: 'ann-1',
    artifactId: 'art-1',
    targetType: 'text_selection',
    targetData: { startOffset: 3, endOffset: 9, selectedText: '选中片段' },
    content: '既有批注',
    authorType: 'user',
    authorId: 'u1',
    status: 'open',
    createdAt: '2026-11-17T00:00:00.000Z',
    ...overrides,
  }
}

/** getDocument 桩：真身同步返回 {promise}（LoadingTask 形态），非 Promise */
function loadingTask(pages: string[]) {
  return { promise: Promise.resolve(pdfDoc(pages)) }
}

/** pdf.js 文档桩：numPages 页，每页一段文本 */
function pdfDoc(pages: string[]) {
  return {
    numPages: pages.length,
    getPage: async (n: number) => ({
      getTextContent: async () => ({ items: pages[n - 1]!.split(' ').map((str) => ({ str })) }),
    }),
  }
}

beforeEach(() => {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => ({ ok: true, arrayBuffer: async () => new ArrayBuffer(8) })),
  )
  listAnnotationsMock.mockReset().mockResolvedValue([])
  createAnnotationMock.mockReset()
  getDocumentMock.mockReset()
  renderAsyncMock.mockReset()
  pptxInitMock.mockReset()
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('DocumentViewer — 格式路由', () => {
  it('不支持格式给显式空态', () => {
    render(<DocumentViewer filePath="a.txt" artifactId="art-1" />)
    expect(screen.getByText(/不支持的文档格式/)).toBeInTheDocument()
  })
})

describe('DocumentViewer — PDF', () => {
  it('成功：逐页文本层渲染', async () => {
    getDocumentMock.mockReturnValue(loadingTask(['page one text', '第二页']))
    render(<DocumentViewer filePath="a.pdf" artifactId="art-1" url="/f/a.pdf" />)
    await waitFor(() => {
      expect(screen.getByTestId('pdf-text-layer')).toBeInTheDocument()
    })
    expect(screen.getByText('第 1 页')).toBeInTheDocument()
    expect(screen.getByText('第 2 页')).toBeInTheDocument()
    expect(screen.getByText('page one text')).toBeInTheDocument()
  })

  it('失败且有 url：降级浏览器内置 iframe 并提示', async () => {
    getDocumentMock.mockRejectedValue(new Error('bad header'))
    render(<DocumentViewer filePath="a.pdf" artifactId="art-1" url="/f/a.pdf" />)
    await waitFor(() => {
      expect(screen.getByTitle('PDF 预览（内置）')).toBeInTheDocument()
    })
    expect(screen.getByText(/PDF 渲染失败，已降级/)).toBeInTheDocument()
  })

  it('失败且无 url：显式失败态；无数据同样走失败分支', async () => {
    getDocumentMock.mockRejectedValue(new Error('x'))
    const { rerender } = render(<DocumentViewer filePath="a.pdf" artifactId="art-1" />)
    await waitFor(() => {
      expect(screen.getByText('PDF 无法预览（缺少取流地址）')).toBeInTheDocument()
    })
    // 无 url 无 content：loadBytes 返回 undefined → 「缺少文档数据」失败
    getDocumentMock.mockClear()
    rerender(<DocumentViewer filePath="b.pdf" artifactId="art-1" />)
    await waitFor(() => {
      expect(screen.getByText(/缺少文档数据/)).toBeInTheDocument()
    })
    expect(getDocumentMock).not.toHaveBeenCalled()
  })
})

describe('DocumentViewer — DOCX / PPTX', () => {
  it('docx 成功：渲染宿主在位；失败给可下载提示', async () => {
    renderAsyncMock.mockImplementation(async (_bytes: unknown, host: HTMLElement) => {
      host.innerHTML = '<p>rendered</p>'
    })
    const { unmount } = render(
      <DocumentViewer filePath="a.docx" artifactId="art-1" content="ZG9jeA==" />,
    )
    await waitFor(() => {
      expect(renderAsyncMock).toHaveBeenCalled()
    })
    expect(screen.getByTestId('docx-host')).toBeInTheDocument()
    unmount()

    renderAsyncMock.mockRejectedValue(new Error('corrupt'))
    render(<DocumentViewer filePath="a.docx" artifactId="art-1" content="ZG9jeA==" />)
    await waitFor(() => {
      expect(screen.getByText(/Word 文档渲染失败/)).toBeInTheDocument()
    })
  })

  it('pptx 成功：预览宿主在位；失败降级页级占位（仍可批注/导出）', async () => {
    pptxInitMock.mockReturnValue({ preview: vi.fn(async () => undefined) })
    const { unmount } = render(
      <DocumentViewer filePath="a.pptx" artifactId="art-1" content="cHBUeA==" />,
    )
    await waitFor(() => {
      expect(pptxInitMock).toHaveBeenCalled()
    })
    expect(screen.getByTestId('pptx-host')).toBeInTheDocument()
    unmount()

    pptxInitMock.mockReturnValue({
      preview: vi.fn(async () => {
        throw new Error('nope')
      }),
    })
    render(<DocumentViewer filePath="a.pptx" artifactId="art-1" content="cHBUeA==" />)
    await waitFor(() => {
      expect(screen.getByTestId('pptx-placeholder')).toBeInTheDocument()
    })
    expect(screen.getByText(/批注与文档分离存储/)).toBeInTheDocument()
  })
})

describe('DocumentViewer — 批注链路', () => {
  it('列表加载：侧栏条目 + 目标摘要；加载失败给可见提示不阻塞', async () => {
    getDocumentMock.mockReturnValue(loadingTask(['text']))
    listAnnotationsMock.mockResolvedValue([makeAnnotation()])
    render(<DocumentViewer filePath="a.pdf" artifactId="art-1" url="/f/a.pdf" />)
    await waitFor(() => {
      expect(screen.getAllByTestId('doc-annotation-item')).toHaveLength(1)
    })
    expect(screen.getByText('既有批注')).toBeInTheDocument()
    expect(screen.getByText(/\[3-9\]/)).toBeInTheDocument()

    listAnnotationsMock.mockRejectedValue(new Error('down'))
    render(<DocumentViewer filePath="b.pdf" artifactId="art-2" url="/f/b.pdf" />)
    await waitFor(() => {
      expect(screen.getByText(/批注加载失败/)).toBeInTheDocument()
    })
  })

  it('选区 → 浮层 → 提交：createAnnotation 落库后条目入列', async () => {
    getDocumentMock.mockReturnValue(loadingTask(['some text here']))
    createAnnotationMock.mockResolvedValue(makeAnnotation({ id: 'ann-2', content: '新批注' }))
    render(<DocumentViewer filePath="a.pdf" artifactId="art-1" url="/f/a.pdf" />)

    // 等文本层渲染完成（pdf 异步链）再模拟选区
    await waitFor(() => {
      expect(screen.getByText('some text here')).toBeInTheDocument()
    })
    // 模拟文本选区（selectionToTarget 真函数读 ownerDocument.getSelection）
    const textEl = screen.getByText('some text here')
    const textNode = textEl.firstChild!
    const selection = document.getSelection()!
    const range = document.createRange()
    range.setStart(textNode, 0)
    range.setEnd(textNode, 4)
    selection.removeAllRanges()
    selection.addRange(range)
    // mouseup 从文本元素冒泡到内层容器（handler 挂在 containerRef 上）
    textEl.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }))

    const composer = await screen.findByTestId('doc-annotation-composer')
    expect(composer).toBeInTheDocument()

    const input = screen.getByTestId('doc-annotation-input') as HTMLTextAreaElement
    await userEvent.type(input, '新批注')
    await userEvent.click(screen.getByTestId('doc-annotation-submit'))

    await waitFor(() => {
      expect(createAnnotationMock).toHaveBeenCalledWith(
        'art-1',
        expect.objectContaining({ content: '新批注', authorType: 'user' }),
      )
    })
    expect(screen.getByText('新批注')).toBeInTheDocument()
  })

  it('提交失败：createAnnotation 拒绝给可见提示，条目不入列', async () => {
    getDocumentMock.mockReturnValue(loadingTask(['text body']))
    createAnnotationMock.mockRejectedValue(new Error('down'))
    render(<DocumentViewer filePath="a.pdf" artifactId="art-1" url="/f/a.pdf" />)
    await waitFor(() => {
      expect(screen.getByText('text body')).toBeInTheDocument()
    })
    const textEl = screen.getByText('text body')
    const selection = document.getSelection()!
    const range = document.createRange()
    range.setStart(textEl.firstChild!, 0)
    range.setEnd(textEl.firstChild!, 4)
    selection.removeAllRanges()
    selection.addRange(range)
    textEl.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }))
    await screen.findByTestId('doc-annotation-composer')
    await userEvent.type(screen.getByTestId('doc-annotation-input'), '将被拒绝')
    await userEvent.click(screen.getByTestId('doc-annotation-submit'))
    await waitFor(() => {
      expect(screen.getByText(/批注保存失败/)).toBeInTheDocument()
    })
    // 失败不入列：列表仍为空态（composer 保留草稿属可重试语义）
    expect(screen.queryAllByTestId('doc-annotation-item')).toHaveLength(0)
  })

  it('导出按钮：有批注时可点并触发 Blob 下载链接', async () => {
    getDocumentMock.mockReturnValue(loadingTask(['text']))
    listAnnotationsMock.mockResolvedValue([makeAnnotation()])
    const createObjectURL = vi.fn(() => 'blob:mock')
    const revokeObjectURL = vi.fn()
    vi.stubGlobal('URL', { ...URL, createObjectURL, revokeObjectURL })
    const clickSpy = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})

    render(<DocumentViewer filePath="docs/spec.pdf" artifactId="art-1" url="/f/a.pdf" />)
    await waitFor(() => {
      expect(screen.getByTestId('doc-annotation-export')).toBeEnabled()
    })
    await userEvent.click(screen.getByTestId('doc-annotation-export'))
    expect(createObjectURL).toHaveBeenCalled()
    expect(clickSpy).toHaveBeenCalled()
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:mock')
  })
})
