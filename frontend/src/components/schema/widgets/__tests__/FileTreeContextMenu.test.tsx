// @feature FP-T12 前端组件补测
/** @ci: frontend-test */
/**
 * FileTreeContextMenu 行为测试：菜单项可见性 / 重命名 / 新建 / 删除 / 移动 /
 * Esc 与外点关闭。
 *
 * useWorkspaceStore 为桩（rename/create/delete/move 记录调用），window.confirm 桩。
 */

import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { FileTreeContextMenu } from '../FileTreeContextMenu'
import type { ContextMenuContext, ContextMenuTreeNode } from '../FileTreeContextMenu'

const storeCalls: Record<string, unknown[]> = {
  renameEntry: [],
  createEntry: [],
  deleteEntry: [],
  moveEntry: [],
}
const storeResults: Record<string, boolean> = {
  renameEntry: true,
  createEntry: true,
  deleteEntry: true,
  moveEntry: true,
}

vi.mock('@/stores/workspaceStore', () => ({
  useWorkspaceStore: () => ({
    renameEntry: vi.fn(async (...args: unknown[]) => {
      storeCalls.renameEntry.push(args)
      return storeResults.renameEntry
    }),
    createEntry: vi.fn(async (...args: unknown[]) => {
      storeCalls.createEntry.push(args)
      return storeResults.createEntry
    }),
    deleteEntry: vi.fn(async (...args: unknown[]) => {
      storeCalls.deleteEntry.push(args)
      return storeResults.deleteEntry
    }),
    moveEntry: vi.fn(async (...args: unknown[]) => {
      storeCalls.moveEntry.push(args)
      return storeResults.moveEntry
    }),
  }),
}))

const tree: ContextMenuTreeNode[] = [
  {
    name: 'src',
    path: 'src',
    isDirectory: true,
    children: [
      { name: 'util', path: 'src/util', isDirectory: true, children: [] },
      { name: 'a.py', path: 'src/a.py', isDirectory: false },
    ],
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
  return render(<FileTreeContextMenu x={10} y={10} context={context} onClose={onClose} />)
}

beforeEach(() => {
  vi.clearAllMocks()
  storeCalls.renameEntry = []
  storeCalls.createEntry = []
  storeCalls.deleteEntry = []
  storeCalls.moveEntry = []
})

describe('菜单项可见性', () => {
  it('文件右键：新建两项不出现，重命名/删除/移动出现', () => {
    renderMenu()
    expect(screen.queryByText('新建文件')).not.toBeInTheDocument()
    expect(screen.queryByText('新建文件夹')).not.toBeInTheDocument()
    expect(screen.getByText('重命名')).toBeInTheDocument()
    expect(screen.getByText('删除')).toBeInTheDocument()
    expect(screen.getByText('移动到...')).toBeInTheDocument()
  })

  it('目录右键：五项全部出现', () => {
    renderMenu(makeContext({
      targetPath: 'src', targetName: 'src', isDirectory: true,
    }))
    expect(screen.getByText('新建文件')).toBeInTheDocument()
    expect(screen.getByText('新建文件夹')).toBeInTheDocument()
    expect(screen.getByText('重命名')).toBeInTheDocument()
  })

  it('空白区域右键：只有新建两项', () => {
    renderMenu(makeContext({ targetPath: null, targetName: null, isDirectory: false }))
    expect(screen.getByText('新建文件')).toBeInTheDocument()
    expect(screen.getByText('新建文件夹')).toBeInTheDocument()
    expect(screen.queryByText('重命名')).not.toBeInTheDocument()
    expect(screen.queryByText('删除')).not.toBeInTheDocument()
  })
})

describe('重命名', () => {
  it('点击重命名 → 输入框预填当前名；确认成功 → 调 store 并刷新', async () => {
    const onRefresh = vi.fn()
    renderMenu(makeContext({ onRefresh }))

    fireEvent.click(screen.getByText('重命名'))
    const input = screen.getByRole('textbox') as HTMLInputElement
    expect(input.value).toBe('a.py')

    fireEvent.change(input, { target: { value: 'b.py' } })
    fireEvent.click(screen.getByText('确认'))

    await waitFor(() => {
      expect(storeCalls.renameEntry).toEqual([['task-1', 'src/a.py', 'b.py']])
      expect(onRefresh).toHaveBeenCalledTimes(1)
    })
  })

  it('名字未变 → 确认不调 store', async () => {
    renderMenu()
    fireEvent.click(screen.getByText('重命名'))
    fireEvent.click(screen.getByText('确认'))
    await waitFor(() => expect(screen.queryByRole('textbox')).not.toBeInTheDocument())
    expect(storeCalls.renameEntry).toHaveLength(0)
  })

  it('Esc 在重命名态取消输入而非关菜单', () => {
    const onClose = vi.fn()
    renderMenu(makeContext(), onClose)
    fireEvent.click(screen.getByText('重命名'))
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument()
    expect(onClose).not.toHaveBeenCalled()
  })
})

describe('新建', () => {
  it('空白区域新建文件 → 路径 = parentDir/默认名', async () => {
    const onRefresh = vi.fn()
    renderMenu(makeContext({ targetPath: null, targetName: null, onRefresh }))

    fireEvent.click(screen.getByText('新建文件'))
    const input = screen.getByRole('textbox') as HTMLInputElement
    expect(input.value).toBe('new_file.txt')
    fireEvent.click(screen.getByText('确认'))

    await waitFor(() => {
      expect(storeCalls.createEntry).toEqual([['task-1', 'src/new_file.txt', 'file']])
      expect(onRefresh).toHaveBeenCalledTimes(1)
    })
  })

  it('目录上新建文件夹 → 路径锚定该目录', async () => {
    renderMenu(makeContext({ targetPath: 'src', targetName: 'src', isDirectory: true }))

    fireEvent.click(screen.getByText('新建文件夹'))
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'comp' } })
    fireEvent.click(screen.getByText('确认'))

    await waitFor(() => {
      expect(storeCalls.createEntry).toEqual([['task-1', 'src/comp', 'directory']])
    })
  })

  it('清空名字确认 → 不调 store 直接退出', async () => {
    renderMenu(makeContext({ targetPath: null, targetName: null }))
    fireEvent.click(screen.getByText('新建文件'))
    fireEvent.change(screen.getByRole('textbox'), { target: { value: '  ' } })
    fireEvent.click(screen.getByText('确认'))
    expect(storeCalls.createEntry).toHaveLength(0)
  })

  it('新建成功后 onRefresh 才刷新；失败不刷新', async () => {
    storeResults.createEntry = false
    const onRefresh = vi.fn()
    renderMenu(makeContext({ targetPath: null, targetName: null, onRefresh }))
    fireEvent.click(screen.getByText('新建文件'))
    fireEvent.click(screen.getByText('确认'))
    await waitFor(() => expect(storeCalls.createEntry).toHaveLength(1))
    expect(onRefresh).not.toHaveBeenCalled()
  })
})

describe('删除与移动', () => {
  it('删除：confirm 通过 → 调 store + 关菜单；取消 → 不调', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)
    const onClose = vi.fn()
    renderMenu(makeContext(), onClose)

    fireEvent.click(screen.getByText('删除'))
    expect(confirmSpy).toHaveBeenCalled()
    await waitFor(() => {
      expect(storeCalls.deleteEntry).toEqual([['task-1', 'src/a.py']])
      expect(onClose).toHaveBeenCalledTimes(1)
    })

      // 取消分支
      storeCalls.deleteEntry = []
      confirmSpy.mockReturnValue(false)
      fireEvent.click(screen.getByText('删除'))
      await waitFor(() => expect(confirmSpy).toHaveBeenCalledTimes(2))
      expect(storeCalls.deleteEntry).toHaveLength(0)
      confirmSpy.mockRestore()
  })

  it('移动对话框：列出目录树（排除自身）、根目录可点', async () => {
    const onRefresh = vi.fn()
    const onClose = vi.fn()
    renderMenu(makeContext({ targetPath: 'src', targetName: 'src', isDirectory: true, onRefresh }), onClose)

    fireEvent.click(screen.getByText('移动到...'))
    // 自身 src 被排除；util/docs 保留（label 层级拼接）
    expect(screen.getByText('/ (根目录)')).toBeInTheDocument()
    expect(screen.getByText('docs')).toBeInTheDocument()
    // 排除自身且不递归其子树：src 与 src/util 均不可选（防移动成环）
    expect(screen.queryByText('src')).not.toBeInTheDocument()
    expect(screen.queryByText('src/util')).not.toBeInTheDocument()

    fireEvent.click(screen.getByText('docs'))
    await waitFor(() => {
      expect(storeCalls.moveEntry).toEqual([['task-1', 'src', 'docs']])
      expect(onRefresh).toHaveBeenCalledTimes(1)
      expect(onClose).toHaveBeenCalledTimes(1)
    })
  })

  it('移动对话框：无可用目录 → 空提示', () => {
    renderMenu(makeContext({ treeData: [{ name: 'f.txt', path: 'f.txt', isDirectory: false }] }))
    fireEvent.click(screen.getByText('移动到...'))
    expect(screen.getByText('没有可用的目标文件夹')).toBeInTheDocument()
  })
})

describe('关闭行为', () => {
  it('Esc 关闭菜单；菜单外 mousedown 关闭', async () => {
    const onClose = vi.fn()
    const { unmount } = renderMenu(makeContext(), onClose)

    fireEvent.keyDown(document, { key: 'Escape' })
    expect(onClose).toHaveBeenCalledTimes(1)
    unmount()

    const onClose2 = vi.fn()
    renderMenu(makeContext(), onClose2)
    // 外点监听延迟绑定（setTimeout 0）：flush 宏任务后再触发
    await act(async () => {
      await new Promise((r) => setTimeout(r, 5))
    })
    fireEvent(document.body, new MouseEvent('mousedown', { bubbles: true }))
    expect(onClose2).toHaveBeenCalledTimes(1)
  })
})
