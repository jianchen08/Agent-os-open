// @feature FP-T12 前端适配（OBS-R258-1 交互反馈即时性收口） | @ci: frontend-test
/**
 * FileTreeContextMenu 写操作 pending 反馈测试（#4）：
 * - store.mutatingPath 命中目标路径 → 对应确认/菜单/目标按钮 disabled + 文案切换
 *   （重命名「保存中...」/ 新建「创建中...」/ 移动「移动中...」/ 删除项转圈禁用）
 * - 写操作进行中：外点与 Esc 不拆菜单（可见 pending 留驻到落定）
 *
 * useWorkspaceStore 为可变桩（mutatingPath 由用例直写模拟 await 窗口）。
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { FileTreeContextMenu } from '../FileTreeContextMenu'
import type { ContextMenuContext, ContextMenuTreeNode } from '../FileTreeContextMenu'

/** 可变 store 桩：用例直写 mutatingPath 模拟写动作 await 窗口 */
const storeStub = {
  mutatingPath: null as string | null,
  renameEntry: vi.fn(async () => true),
  createEntry: vi.fn(async () => true),
  deleteEntry: vi.fn(async () => true),
  moveEntry: vi.fn(async () => true),
}

vi.mock('@/stores/workspaceStore', () => ({
  useWorkspaceStore: () => storeStub,
}))

const tree: ContextMenuTreeNode[] = [
  {
    name: 'src',
    path: 'src',
    isDirectory: true,
    children: [{ name: 'a.py', path: 'src/a.py', isDirectory: false }],
  },
  { name: 'docs', path: 'docs', isDirectory: true, children: [] },
]

function makeContext(overrides: Partial<ContextMenuContext> = {}): ContextMenuContext {
  return {
    containerTaskId: 'task-1',
    targetPath: 'src/a.py',
    targetName: 'a.py',
    isDirectory: false,
    parentDir: 'src',
    treeData: tree,
    onRefresh: vi.fn(),
    ...overrides,
  }
}

function renderMenu(context: ContextMenuContext = makeContext(), onClose = vi.fn()) {
  return { onClose, ...render(<FileTreeContextMenu x={10} y={10} context={context} onClose={onClose} />) }
}

beforeEach(() => {
  vi.clearAllMocks()
  storeStub.mutatingPath = null
})

describe('FileTreeContextMenu 写操作 pending 反馈', () => {
  it('重命名 await 窗口：确认按钮 disabled +「保存中...」，取消钮同禁', () => {
    const { rerender } = renderMenu()
    fireEvent.click(screen.getByText('重命名'))
    // 空闲态：确认可用
    expect(screen.getByRole('button', { name: '确认' })).toBeEnabled()

    storeStub.mutatingPath = 'src/a.py'
    rerender(<FileTreeContextMenu x={10} y={10} context={makeContext()} onClose={vi.fn()} />)
    expect(screen.getByRole('button', { name: '保存中...' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '取消' })).toBeDisabled()
  })

  it('新建 await 窗口：确认按钮 disabled +「创建中...」', () => {
    const { rerender } = renderMenu(makeContext({ targetPath: 'src', targetName: 'src', isDirectory: true }))
    fireEvent.click(screen.getByText('新建文件'))
    expect(screen.getByRole('button', { name: '确认' })).toBeEnabled()

    // store 置位的目标 = baseDir/name 全路径
    storeStub.mutatingPath = 'src/new_file.txt'
    rerender(
      <FileTreeContextMenu
        x={10}
        y={10}
        context={makeContext({ targetPath: 'src', targetName: 'src', isDirectory: true })}
        onClose={vi.fn()}
      />,
    )
    expect(screen.getByRole('button', { name: '创建中...' })).toBeDisabled()
  })

  it('删除 await 窗口：菜单删除项禁用转圈（图标换 Loader2，aria 禁用）', () => {
    const { rerender } = renderMenu()
    expect(screen.getByRole('button', { name: /删除/ })).toBeEnabled()

    storeStub.mutatingPath = 'src/a.py'
    rerender(<FileTreeContextMenu x={10} y={10} context={makeContext()} onClose={vi.fn()} />)
    const deleteBtn = screen.getByRole('button', { name: /删除/ })
    expect(deleteBtn).toBeDisabled()
    // 图标位换转圈（animate-spin 类）
    expect(deleteBtn.querySelector('.animate-spin')).not.toBeNull()
  })

  it('移动 await 窗口：目标选项 disabled +「移动中...」', () => {
    const { rerender } = renderMenu()
    fireEvent.click(screen.getByText('移动到...'))
    expect(screen.getByRole('button', { name: '/ (根目录)' })).toBeEnabled()

    storeStub.mutatingPath = 'src/a.py'
    rerender(<FileTreeContextMenu x={10} y={10} context={makeContext()} onClose={vi.fn()} />)
    expect(screen.getByRole('button', { name: '移动中...' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'docs' })).toBeDisabled()
  })

  it('写操作进行中：外点不关菜单（可见 pending 留驻），空闲态外点照常关', async () => {
    const { onClose, rerender } = renderMenu()

    // 空闲态：外点关闭（监听器延迟 0ms 绑定，等挂载）
    await waitFor(() => {
      fireEvent.mouseDown(document.body)
      expect(onClose).toHaveBeenCalledTimes(1)
    })

    // pending 态：外点不拆
    storeStub.mutatingPath = 'src/a.py'
    rerender(<FileTreeContextMenu x={10} y={10} context={makeContext()} onClose={onClose} />)
    fireEvent.mouseDown(document.body)
    fireEvent.keyDown(document.body, { key: 'Escape' })
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('pending 未命中目标路径（他树操作）：本菜单不受影响', () => {
    renderMenu()
    storeStub.mutatingPath = 'other/path.txt'
    expect(screen.getByRole('button', { name: /删除/ })).toBeEnabled()
  })
})
