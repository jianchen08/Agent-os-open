/** @feature FP-0.2.多设备目录树 目录树选择弹窗 | @ci: frontend-test */
/** @feature 多端目录树选择模态框（工作空间选择器复用 FileTreeWidget） | @ci frontend-test */
/**
 * DirectoryTreePickerModal 契约：
 * - 打开即渲染 fsbrowse:// 目录树（根层=本地盘+设备分支锚目录），关闭不渲染
 * - 点目录行 → 底部路径框跟随当前目录；「选定」回填 onPick(path) 并关闭
 * - 路径框可手改（浏览+手输两路并存）；取消只关闭不回填
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi, beforeEach } from 'vitest'
const mockGet = vi.fn()
vi.mock('@/services/api/client', () => ({
  default: { get: (...args: unknown[]) => mockGet(...args) },
}))
vi.mock('@/services/api/tasks', () => ({
  pauseTask: vi.fn(),
  resumeTask: vi.fn(),
}))
vi.mock('@/services/schema/parser', () => ({
  parseDataSourceRef: (ref: string) => ({ endpoint: ref, params: {} }),
  resolveDataSource: (ref: { endpoint: string }) => ({ endpoint: ref.endpoint, params: {} }),
}))
vi.mock('@/stores/layoutModeStore', () => ({
  useLayoutModeStore: { getState: () => ({ workspaceTabs: [], setActiveTab: vi.fn(), addWorkspaceTab: vi.fn() }), setState: vi.fn() },
}))
vi.mock('@/components/schema/widgets/CreateTaskFormModal', () => ({
  CreateTaskFormModal: () => null,
}))
vi.mock('@/components/schema/widgets/FileTreeContextMenu', () => ({
  FileTreeContextMenu: () => null,
}))
import { DirectoryTreePickerModal } from '../DirectoryTreePickerModal'

describe('DirectoryTreePickerModal — 多端目录树选择', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    // 根层=目录项；子层=文件项（子层路径必异于父层，真实 fs 契约；目录子项会让
    // 懒加载合并按路径递归取层，mock 数据必须可终结）
    mockGet.mockImplementation((_url: string, config?: { params?: { path?: string } }) => {
      const path = config?.params?.path ?? ''
      if (!path) {
        return Promise.resolve({
          data: {
            entries: [{ name: 'C:\\projects', type: 'directory', path: 'C:\\projects' }],
            truncated: false,
          },
        })
      }
      return Promise.resolve({
        data: { entries: [{ name: 'demo.txt', type: 'file', path: `${path}\\demo.txt` }], truncated: false },
      })
    })
  })

  it('open=false 不渲染；open=true 渲染目录树并拉根层', async () => {
    const { container, rerender } = render(
      <DirectoryTreePickerModal open={false} onClose={vi.fn()} onPick={vi.fn()} />,
    )
    expect(container.querySelector('[data-testid="directory-picker-modal"]')).toBeNull()
    expect(mockGet).not.toHaveBeenCalled()

    rerender(<DirectoryTreePickerModal open onClose={vi.fn()} onPick={vi.fn()} />)
    expect(await screen.findByText('C:\\projects')).toBeInTheDocument()
    expect(mockGet).toHaveBeenCalledWith(
      'fsbrowse://roots',
      expect.objectContaining({ params: expect.anything() }),
    )
  })

  it('点目录行 → 路径框跟随；「选定」回填 onPick 并关闭', async () => {
    const onPick = vi.fn()
    const onClose = vi.fn()
    render(<DirectoryTreePickerModal open onClose={onClose} onPick={onPick} />)

    expect(await screen.findByText('C:\\projects')).toBeInTheDocument()
    fireEvent.click(screen.getByText('C:\\projects'))

    const input = screen.getByTestId('directory-picker-current') as HTMLInputElement
    await waitFor(() => expect(input.value).toBe('C:\\projects'))

    fireEvent.click(screen.getByTestId('directory-picker-confirm'))
    expect(onPick).toHaveBeenCalledWith('C:\\projects')
    expect(onClose).toHaveBeenCalled()
  })

  it('路径框可手改；改后「选定」以手改值为准', async () => {
    const onPick = vi.fn()
    render(<DirectoryTreePickerModal open onClose={vi.fn()} onPick={onPick} />)

    expect(await screen.findByText('C:\\projects')).toBeInTheDocument()
    fireEvent.click(screen.getByText('C:\\projects'))
    const input = screen.getByTestId('directory-picker-current') as HTMLInputElement
    await waitFor(() => expect(input.value).toBe('C:\\projects'))

    fireEvent.change(input, { target: { value: 'C:\\projects\\demo' } })
    fireEvent.click(screen.getByTestId('directory-picker-confirm'))
    expect(onPick).toHaveBeenCalledWith('C:\\projects\\demo')
  })

  it('取消按钮只关闭不回填', async () => {
    const onPick = vi.fn()
    const onClose = vi.fn()
    render(<DirectoryTreePickerModal open onClose={onClose} onPick={onPick} />)

    expect(await screen.findByText('C:\\projects')).toBeInTheDocument()
    fireEvent.click(screen.getByTestId('directory-picker-cancel'))
    expect(onClose).toHaveBeenCalled()
    expect(onPick).not.toHaveBeenCalled()
  })
})
