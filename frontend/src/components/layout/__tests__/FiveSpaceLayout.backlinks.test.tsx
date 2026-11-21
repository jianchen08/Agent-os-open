/** @feature FP-0.2.四 前端Schema（双链反链侧栏装配） @vision V6 可即用 @ci frontend-test */
/**
 * FiveSpaceLayout markdown 预览分支的反链侧栏装配测试（双链 P1）。
 *
 * 判据（用户裁定 2026-11-21 授权 FiveSpaceLayout 最小挂载）：
 * - __file_editor__ 页签文件命中 markdown_preview（.md）→ FilePreview 与
 *   BacklinksPanel 并排装配，BacklinksPanel 收到仓库相对路径 filePath
 * - 非 markdown 预览（.png → image_preview）不挂反链侧栏（装配面不扩张）
 *
 * 打桩边界：FilePreview / BacklinksPanel 本体各有专项测试族，此处只断装配层
 * 传参，不进组件内部。
 */
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { registerFileEditor, removeFileEditorData } from '@/stores/fileEditorRegistry'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import { FiveSpaceLayout } from '../FiveSpaceLayout'
import { chatContent, ResizeObserverStub, setViewportWidth, sidebarContent } from './helpers/fiveSpaceTestUtils'
import type { WorkspaceTab } from '@/types/layout'

vi.mock('@/components/workspace/FilePreview', () => ({
  FilePreview: (props: { filePath?: string }) => (
    <div data-testid="mock-file-preview" data-file={props.filePath ?? ''} />
  ),
}))
vi.mock('@/components/shared/markdown/BacklinksPanel', () => ({
  BacklinksPanel: (props: { filePath?: string }) => (
    <div data-testid="mock-backlinks-panel" data-file-path={props.filePath ?? ''} />
  ),
}))
vi.mock('@/services/api/costControl', () => ({
  getBudgetStatus: async () => null,
}))
vi.mock('@/services/workspacePanelOpener', () => ({
  openDeclaredPluginPage: vi.fn(() => true),
}))
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

function seedEditorTab(tabId: string, filePath: string): void {
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
  for (const tabId of seededTabs) removeFileEditorData(tabId)
  seededTabs.length = 0
})

describe('FiveSpaceLayout — markdown 预览反链侧栏装配', () => {
  it('md 文件页签：FilePreview 与 BacklinksPanel 并排装配，侧栏收仓库相对路径', async () => {
    seedEditorTab('file-md-1', 'docs/decisions/x.md')
    renderLayout()
    // FilePreview 是 lazy 动态组件：findByTestId 等 Suspense 解析（同步查必 null）
    const preview = await screen.findByTestId('mock-file-preview')
    const panel = await screen.findByTestId('mock-backlinks-panel')
    expect(preview.getAttribute('data-file')).toBe('docs/decisions/x.md')
    expect(panel.getAttribute('data-file-path')).toBe('docs/decisions/x.md')
  })

  it('非 md 预览（image_preview）不挂反链侧栏', async () => {
    seedEditorTab('file-img-1', 'assets/logo.png')
    renderLayout()
    await screen.findByTestId('mock-file-preview')
    expect(screen.queryByTestId('mock-backlinks-panel')).toBeNull()
  })
})
