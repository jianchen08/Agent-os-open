/** @feature FP-0.2.四 前端Schema（文档查看器装配） @vision V6 可即用 @ci frontend-test */
/**
 * FiveSpaceLayout 文档查看器页签装配测试（doc_viewer 懒加载分支）。
 *
 * 判据（方案 §3.6 + ADR 2026-10-24-file-viewer-declarative）：
 * - __file_editor__ 页签文件命中 document_viewer（pdf/docx/pptx）→ 懒挂载
 *   DocumentViewer，不落 CodeEditor 兜底
 * - 取流 URL：附件 blob url 优先；无 url 时按 container_task_id 拼
 *   workspaces file-content 端点（path 编码）
 * - 批注键 artifactId = file:<containerTaskId>:<filePath>（稳定键）
 *
 * 打桩边界：DocumentViewer 本体（pdfjs/docx-preview 重依赖，主会话另有专项
 * 测试族）——此处只断装配层传参，不进查看器内部。
 */
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { registerFileEditor, removeFileEditorData } from '@/stores/fileEditorRegistry'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import { FiveSpaceLayout } from '../FiveSpaceLayout'
import { chatContent, ResizeObserverStub, setViewportWidth, sidebarContent } from './helpers/fiveSpaceTestUtils'
import type { WorkspaceTab } from '@/types/layout'

vi.mock('@/components/workspace/docViewer/DocumentViewer', () => ({
  DocumentViewer: (props: { filePath?: string; url?: string; artifactId?: string }) => (
    <div
      data-testid="mock-document-viewer"
      data-file={props.filePath ?? ''}
      data-url={props.url ?? ''}
      data-artifact-id={props.artifactId ?? ''}
    />
  ),
}))
vi.mock('@/services/api/costControl', () => ({
  getBudgetStatus: async () => null,
}))
vi.mock('@/services/workspacePanelOpener', () => ({
  openDeclaredPluginPage: vi.fn(() => true),
}))
// apiClient 网络层打桩（外部依赖：HTTP；编辑器轮询/保存不触网）
const apiMock = vi.hoisted(() => ({ get: vi.fn(), put: vi.fn(), post: vi.fn() }))
vi.mock('@/services/api/client', () => ({ default: apiMock, apiClient: apiMock }))

function renderLayout() {
  return render(
    <MemoryRouter>
      <FiveSpaceLayout chatContent={chatContent} sidebarContent={sidebarContent} />
    </MemoryRouter>,
  )
}

const seededTabs: string[] = []

function seedEditorTab(tabId: string, filePath: string, extra: Record<string, unknown> = {}): void {
  const tab: WorkspaceTab = {
    id: tabId,
    title: filePath,
    moduleId: '__file_editor__',
    isActive: true,
    isPinned: false,
  }
  useLayoutModeStore.setState({ workspaceTabs: [tab], visitedTabIds: [tab.id] })
  registerFileEditor(tabId, {
    filePath,
    fileName: filePath.split('/').pop() ?? filePath,
    content: '',
    containerTaskId: 'task-doc-9',
    ...extra,
  })
  seededTabs.push(tabId)
}

beforeEach(() => {
  globalThis.ResizeObserver = ResizeObserverStub as never
  setViewportWidth(1280)
  useLayoutModeStore.setState({
    workspaceTabs: [],
    floatingWindows: [],
    fullscreenActive: false,
    fullscreenTitle: null,
    fullscreenContent: null,
    workspaceDataVersion: 0,
    pendingInteractions: [],
  })
  apiMock.get.mockReset().mockResolvedValue({ data: { success: true, content: '' } })
  apiMock.put.mockReset()
  apiMock.post.mockReset()
})

afterEach(() => {
  for (const id of seededTabs.splice(0)) removeFileEditorData(id)
})

describe('FiveSpaceLayout 文档查看器页签（document_viewer 装配）', () => {
  it('docx 文件路由到 DocumentViewer：url 按 container_task_id 拼取流端点，path 编码；批注键稳定', async () => {
    // 注：.pdf 先命中内置 pdf_preview（注册表序），document_viewer 分支以
    // .docx/.pptx 为可达路径——此处取 docx 验证无 url 的 workspace 拼接
    seedEditorTab('doc-1', 'docs/合同 v1.docx')
    renderLayout()

    const viewer = await screen.findByTestId('mock-document-viewer')
    expect(viewer.getAttribute('data-file')).toBe('docs/合同 v1.docx')
    const url = viewer.getAttribute('data-url') ?? ''
    expect(url).toContain('task-doc-9')
    expect(url).toContain(encodeURIComponent('docs/合同 v1.docx'))
    expect(viewer.getAttribute('data-artifact-id')).toBe('file:task-doc-9:docs/合同 v1.docx')
    // 文档查看器路由不落代码编辑器兜底
    expect(screen.queryByTestId('mock-code-editor')).toBeNull()
  })

  it('附件 blob url 优先于 workspace 拼接（editorData.url 命中直用）', async () => {
    seedEditorTab('doc-2', 'slides/评审.pptx', { url: 'blob:attachment-1' })
    renderLayout()

    const viewer = await screen.findByTestId('mock-document-viewer')
    expect(viewer.getAttribute('data-url')).toBe('blob:attachment-1')
    // 附件直连不再拼 workspace 端点
    expect(viewer.getAttribute('data-url')).not.toContain('task-doc-9')
  })
})
