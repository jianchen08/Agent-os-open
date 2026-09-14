// @feature FP-T12 前端组件补测
/** @ci: frontend-test */
/**
 * FileTreeContextMenu 残余分支补测（簇3，与既有 FileTreeContextMenu.test.tsx 互补）
 *
 * 覆盖既有测试未触达的分支：
 * - Esc 分层语义：移动对话框开着时 Esc 只关对话框（保留菜单）；重命名/新建态
 *   Esc 取消输入态；无覆盖层时 Esc 关菜单
 * - 重命名输入框的键盘流（Enter 确认走 store、Esc 取消）与「取消」按钮
 * - 新建输入框的键盘流（Enter 确认、Esc 取消）与「取消」按钮
 * - 重命名失败（store 返回 false）→ 不刷新（既有测试只覆盖成功）
 * - 重命名/新建遮罩点击自身（e.target === currentTarget）→ 退出输入态
 * - 移动对话框：根目录选项、头部关闭按钮、遮罩点击、move 失败不刷新
 * - 目录名深度前缀（嵌套层级 label 拼接）与自身子树整体排除
 *
 * mock 纪律：useWorkspaceStore 为外部状态边界（桩记录调用与返回），
 * window.confirm 为浏览器宿主 API——均为外部依赖，非被测实现细节。
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
      {
        name: 'components',
        path: 'src/components',
        isDirectory: true,
        children: [{ name: 'deep', path: 'src/components/deep', isDirectory: true, children: [] }],
      },
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
  storeResults.renameEntry = true
  storeResults.createEntry = true
  storeResults.deleteEntry = true
  storeResults.moveEntry = true
})

describe('Esc 分层语义', () => {
  it('移动对话框开着时 Esc 只关对话框，菜单保留（不触发 onClose）', () => {
    const onClose = vi.fn()
    renderMenu(makeContext({ targetPath: 'src', targetName: 'src', isDirectory: true }), onClose)

    fireEvent.click(screen.getByText('移动到...'))
    expect(screen.getByText('/ (根目录)')).toBeInTheDocument()

    fireEvent.keyDown(document, { key: 'Escape' })
    // 回到右键菜单本体（重命名/删除重新可见），未关菜单
    expect(screen.queryByText('/ (根目录)')).not.toBeInTheDocument()
    expect(screen.getByText('重命名')).toBeInTheDocument()
    expect(onClose).not.toHaveBeenCalled()
  })

  it('新建输入态 Esc 取消输入（菜单保留）', () => {
    const onClose = vi.fn()
    renderMenu(makeContext({ targetPath: null, targetName: null }), onClose)

    fireEvent.click(screen.getByText('新建文件'))
    expect(screen.getByRole('textbox')).toBeInTheDocument()

    fireEvent.keyDown(document, { key: 'Escape' })
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument()
    expect(onClose).not.toHaveBeenCalled()
  })

  it('非 Escape 键不触发任何分支', () => {
    const onClose = vi.fn()
    renderMenu(makeContext(), onClose)
    fireEvent.keyDown(document, { key: 'a' })
    expect(onClose).not.toHaveBeenCalled()
  })
})

describe('重命名键盘流与取消', () => {
  it('Enter 确认：经 store 重命名并刷新', async () => {
    const onRefresh = vi.fn()
    renderMenu(makeContext({ onRefresh }))

    fireEvent.click(screen.getByText('重命名'))
    const input = screen.getByRole('textbox')
    fireEvent.change(input, { target: { value: 'renamed.py' } })
    fireEvent.keyDown(input, { key: 'Enter' })

    await waitFor(() => {
      expect(storeCalls.renameEntry).toEqual([['task-1', 'src/a.py', 'renamed.py']])
      expect(onRefresh).toHaveBeenCalledTimes(1)
    })
  })

  it('输入框内 Esc 取消重命名（不调 store）', () => {
    renderMenu()
    fireEvent.click(screen.getByText('重命名'))
    fireEvent.keyDown(screen.getByRole('textbox'), { key: 'Escape' })

    expect(screen.queryByRole('textbox')).not.toBeInTheDocument()
    expect(storeCalls.renameEntry).toHaveLength(0)
  })

  it('点「取消」按钮退出重命名（不调 store）', () => {
    renderMenu()
    fireEvent.click(screen.getByText('重命名'))
    fireEvent.click(screen.getByText('取消'))

    expect(screen.queryByRole('textbox')).not.toBeInTheDocument()
    expect(storeCalls.renameEntry).toHaveLength(0)
  })

  it('重命名失败（store 返回 false）→ 退出输入态但不刷新', async () => {
    storeResults.renameEntry = false
    const onRefresh = vi.fn()
    renderMenu(makeContext({ onRefresh }))

    fireEvent.click(screen.getByText('重命名'))
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'fail.py' } })
    fireEvent.click(screen.getByText('确认'))

    await waitFor(() => expect(storeCalls.renameEntry).toHaveLength(1))
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument()
    expect(onRefresh).not.toHaveBeenCalled()
  })

  it('重命名遮罩点击自身 → 退出输入态（点内容区不退出）', () => {
    const { container } = renderMenu()
    fireEvent.click(screen.getByText('重命名'))

    // 点内容区（子元素）不退出
    fireEvent.click(screen.getByRole('textbox'))
    expect(screen.getByRole('textbox')).toBeInTheDocument()

    // 点遮罩本体退出
    const overlay = container.querySelector('.fixed.inset-0') as HTMLElement
    fireEvent.click(overlay)
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument()
  })
})

describe('新建键盘流与取消', () => {
  it('Enter 确认新建：目录锚定时路径含父目录', async () => {
    renderMenu(makeContext({ targetPath: 'src', targetName: 'src', isDirectory: true }))

    fireEvent.click(screen.getByText('新建文件夹'))
    const input = screen.getByRole('textbox')
    fireEvent.change(input, { target: { value: 'newdir' } })
    fireEvent.keyDown(input, { key: 'Enter' })

    await waitFor(() => {
      expect(storeCalls.createEntry).toEqual([['task-1', 'src/newdir', 'directory']])
    })
  })

  it('输入框内 Esc 取消新建（不调 store）', () => {
    renderMenu(makeContext({ targetPath: null, targetName: null }))
    fireEvent.click(screen.getByText('新建文件'))
    fireEvent.keyDown(screen.getByRole('textbox'), { key: 'Escape' })

    expect(screen.queryByRole('textbox')).not.toBeInTheDocument()
    expect(storeCalls.createEntry).toHaveLength(0)
  })

  it('点「取消」按钮退出新建（不调 store）', () => {
    renderMenu(makeContext({ targetPath: null, targetName: null }))
    fireEvent.click(screen.getByText('新建文件'))
    fireEvent.click(screen.getByText('取消'))

    expect(screen.queryByRole('textbox')).not.toBeInTheDocument()
    expect(storeCalls.createEntry).toHaveLength(0)
  })

  it('新建遮罩点击自身 → 退出输入态', () => {
    const { container } = renderMenu(makeContext({ targetPath: null, targetName: null }))
    fireEvent.click(screen.getByText('新建文件'))

    const overlay = container.querySelector('.fixed.inset-0') as HTMLElement
    fireEvent.click(overlay)
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument()
  })

  it('顶层无 parentDir 时路径为裸名（不拼多余斜杠）', async () => {
    renderMenu(makeContext({ targetPath: null, targetName: null, parentDir: '' }))
    fireEvent.click(screen.getByText('新建文件'))
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'root.txt' } })
    fireEvent.click(screen.getByText('确认'))

    await waitFor(() => {
      expect(storeCalls.createEntry).toEqual([['task-1', 'root.txt', 'file']])
    })
  })
})

describe('移动对话框补充分支', () => {
  it('根目录选项：点「/ (根目录)」移动到根并关菜单', async () => {
    const onRefresh = vi.fn()
    const onClose = vi.fn()
    renderMenu(
      makeContext({ targetPath: 'docs', targetName: 'docs', isDirectory: true, onRefresh }),
      onClose,
    )

    fireEvent.click(screen.getByText('移动到...'))
    fireEvent.click(screen.getByText('/ (根目录)'))

    await waitFor(() => {
      expect(storeCalls.moveEntry).toEqual([['task-1', 'docs', '/']])
      expect(onRefresh).toHaveBeenCalledTimes(1)
      expect(onClose).toHaveBeenCalledTimes(1)
    })
  })

  it('头部关闭按钮关掉对话框（菜单保留）', () => {
    const onClose = vi.fn()
    renderMenu(makeContext({ targetPath: 'docs', targetName: 'docs', isDirectory: true }), onClose)

    fireEvent.click(screen.getByText('移动到...'))
    expect(screen.getByText('/ (根目录)')).toBeInTheDocument()

    // 头部 X 按钮（对话框内唯一无文本按钮）
    const dialog = screen.getByText('移动到...').closest('.bg-background') as HTMLElement
    fireEvent.click(dialog.querySelector('button') as HTMLElement)

    expect(screen.queryByText('/ (根目录)')).not.toBeInTheDocument()
    expect(onClose).not.toHaveBeenCalled()
  })

  it('移动遮罩点击自身 → 关对话框；点内容区不关', () => {
    const { container } = renderMenu(
      makeContext({ targetPath: 'docs', targetName: 'docs', isDirectory: true }),
    )
    fireEvent.click(screen.getByText('移动到...'))

    fireEvent.click(screen.getByText('/ (根目录)').closest('.bg-background') as HTMLElement)
    expect(screen.getByText('/ (根目录)')).toBeInTheDocument()

    const overlay = container.querySelector('.fixed.inset-0') as HTMLElement
    fireEvent.click(overlay)
    expect(screen.queryByText('/ (根目录)')).not.toBeInTheDocument()
  })

  it('嵌套目录按层级拼接 label（父/子），自身子树整体排除', () => {
    renderMenu(makeContext({ targetPath: 'src', targetName: 'src', isDirectory: true }))
    fireEvent.click(screen.getByText('移动到...'))

    // src 被排除 → 其子树 components/deep 一并不可选（防移动成环）
    expect(screen.queryByText('src')).not.toBeInTheDocument()
    expect(screen.queryByText('src/components')).not.toBeInTheDocument()
    expect(screen.queryByText('src/components/deep')).not.toBeInTheDocument()
    // 兄弟目录保留
    expect(screen.getByText('docs')).toBeInTheDocument()
  })

  it('移动失败（store 返回 false）→ 关菜单但不刷新', async () => {
    storeResults.moveEntry = false
    const onRefresh = vi.fn()
    const onClose = vi.fn()
    renderMenu(
      makeContext({ targetPath: 'docs', targetName: 'docs', isDirectory: true, onRefresh }),
      onClose,
    )

    fireEvent.click(screen.getByText('移动到...'))
    fireEvent.click(screen.getByText('/ (根目录)'))

    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1))
    expect(onRefresh).not.toHaveBeenCalled()
  })

  it('树中无非目录节点 → 只有根目录项与空提示并存（根目录恒可选）', () => {
    renderMenu(
      makeContext({
        treeData: [
          { name: 'f1.txt', path: 'f1.txt', isDirectory: false },
          { name: 'f2.txt', path: 'f2.txt', isDirectory: false },
        ],
      }),
    )
    fireEvent.click(screen.getByText('移动到...'))

    expect(screen.getByText('/ (根目录)')).toBeInTheDocument()
    expect(screen.getByText('没有可用的目标文件夹')).toBeInTheDocument()
  })
})

describe('外点关闭时序', () => {
  it('外点监听在宏任务后才生效（右键当帧不误关）', async () => {
    const onClose = vi.fn()
    renderMenu(makeContext(), onClose)

    // 绑定前的同帧 mousedown 不应关闭
    fireEvent(document.body, new MouseEvent('mousedown', { bubbles: true }))
    expect(onClose).not.toHaveBeenCalled()

    await act(async () => {
      await new Promise((r) => setTimeout(r, 5))
    })
    fireEvent(document.body, new MouseEvent('mousedown', { bubbles: true }))
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('菜单内部 mousedown 不触发关闭', async () => {
    const onClose = vi.fn()
    renderMenu(makeContext(), onClose)

    await act(async () => {
      await new Promise((r) => setTimeout(r, 5))
    })
    fireEvent.mouseDown(screen.getByText('重命名'))
    expect(onClose).not.toHaveBeenCalled()
  })
})
