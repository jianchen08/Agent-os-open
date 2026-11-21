/** @feature FP-0.2.四 前端Schema（任务现场） @vision V6 可即用 @ci frontend-test */
/**
 * 产物面板测试（任务现场批 4）。
 *
 * 判据（方案 §3.4/§3.5 + 组件头注释）：
 * 1. 变更清单按 file_write 聚合：新增/修改/删除三态徽标、末轮 +N -M、多轮「N 轮」
 * 2. 展开行（L1）：产出工具、首末净差（LCS 展开才算）、首末内容 diff、
 *    历史轮次（L2）逐轮摘要与增删；再点收起
 * 3. 打开文件按钮：注入回调优先；未注入回落全局注册表回调
 * 4. 悬浮标记：不可内嵌类型给「可查看」/超 5MB 给「大文件」角标；删除行无打开按钮
 */
import { fireEvent, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { TaskSceneArtifacts } from '@/components/workspace/taskScene/TaskSceneArtifacts'
import type { SceneActivity } from '@/components/workspace/taskScene/sceneActivities'
import type * as toolCardRegistry from '@/utils/toolCardRegistry'

vi.mock('@/components/schema/widgets/FileTreeWidget', () => ({
  FileTreeWidget: vi.fn(({ dataSource, nodeTitleField }: { dataSource: string; nodeTitleField?: string }) => (
    <div data-testid="file-tree-stub" data-source={dataSource} data-title-field={nodeTitleField ?? ''} />
  )),
}))

const mockGlobalOpen = vi.fn()
vi.mock('@/utils/toolCardRegistry', async (importOriginal) => ({
  ...(await importOriginal<typeof toolCardRegistry>()),
  getGlobalOpenFileCallback: vi.fn(() => mockGlobalOpen),
}))

let callSeq = 0

function writeActivity(p: {
  path: string
  oldContent: string | null
  newContent: string | null
  added?: number
  removed?: number
  size?: number
}): SceneActivity {
  callSeq += 1
  return {
    callId: `call-w${callSeq}`,
    toolName: 'file_write',
    args: { path: p.path },
    state: 'done',
    category: 'file',
    summary: 'file_write',
    startedAt: `2026-11-16T10:0${callSeq}:00.000Z`,
    messageId: 'm1',
    resultData: {
      file: p.path,
      old_content: p.oldContent,
      new_content: p.newContent,
      ...(p.added !== undefined ? { added: p.added } : {}),
      ...(p.removed !== undefined ? { removed: p.removed } : {}),
      ...(p.size !== undefined ? { size: p.size } : {}),
    },
  } as SceneActivity
}

function deleteActivity(path: string): SceneActivity {
  callSeq += 1
  return {
    callId: `call-d${callSeq}`,
    toolName: 'delete_file',
    args: { path },
    state: 'done',
    category: 'file',
    summary: 'delete_file',
    messageId: 'm1',
    resultData: { results: [{ path, deleted: true }] },
  } as SceneActivity
}

const multiRoundWrites: SceneActivity[] = [
  writeActivity({ path: 'docs/a.md', oldContent: '', newContent: '# v1\n', added: 2, removed: 0, size: 6 }),
  writeActivity({
    path: 'docs/a.md',
    oldContent: '# v1\n',
    newContent: '# v1\n## v2\n',
    added: 1,
    removed: 0,
    size: 12,
  }),
]

describe('TaskSceneArtifacts — 变更清单（L0）', () => {
  beforeEach(() => {
    mockGlobalOpen.mockClear()
  })

  it('按路径聚合：多轮写入归一行，末轮增删与「2 轮」徽标；汇总行计数正确', () => {
    render(
      <TaskSceneArtifacts
        taskId="t1"
        activities={[
          ...multiRoundWrites,
          // 只报 added 不报 removed：L0/轮次的 -0 走 ?? 兜底
          writeActivity({ path: 'src/main.rs', oldContent: 'fn a() {}', newContent: 'fn ab() {}', added: 1, size: 20 }),
          deleteActivity('old.txt'),
        ]}
      />,
    )

    // 汇总行：a.md 首轮 old='' 判新增 → 新增 1 / 修改 1（main.rs）/ 删除 1，共 3 个文件（a.md 两轮归一）
    expect(screen.getByText('新增 1')).toBeInTheDocument()
    expect(screen.getByText('修改 1')).toBeInTheDocument()
    expect(screen.getByText('删除 1')).toBeInTheDocument()
    expect(screen.getByText('3 个文件')).toBeInTheDocument()

    const rows = screen.getAllByTestId('scene-change-row')
    expect(rows).toHaveLength(3)

    const addedRow = rows[0] as HTMLElement
    expect(within(addedRow).getByTestId('scene-change-status')).toHaveTextContent('新增')
    // 末轮上报的增删（非累计）：第二轮 +1 -0
    expect(within(addedRow).getByText('+1')).toBeInTheDocument()
    expect(within(addedRow).getByText('-0')).toBeInTheDocument()
    expect(within(addedRow).getByText('2 轮')).toBeInTheDocument()

    expect(within(rows[2] as HTMLElement).getByTestId('scene-change-status')).toHaveTextContent('删除')
    // 删除行不给打开按钮
    expect(within(rows[2] as HTMLElement).queryByTestId('scene-change-open')).toBeNull()

      // 文件行主位是真实文件名（修「未命名」）
      expect(within(rows[0] as HTMLElement).getByTestId('scene-change-name')).toHaveTextContent('a.md')
      expect(within(rows[0] as HTMLElement).getByTestId('scene-change-dir')).toHaveTextContent('docs')
      // 「其它文件（按目录）」默认收起：展开前不挂 file_tree（同一区块的第二视角）
      expect(screen.queryByTestId('file-tree-stub')).toBeNull()
      fireEvent.click(screen.getByTestId('task-scene-toggle-tree'))
      const stub = screen.getByTestId('file-tree-stub')
      expect(stub.getAttribute('data-source')).toBe('workspace://t1')
      // nodeTitleField=name 是「未命名」修复的契约锚（懒加载树节点字段是 name）
      expect(stub.getAttribute('data-title-field')).toBe('name')
  })

  it('空清单给空态文案', () => {
    render(<TaskSceneArtifacts taskId="t2" activities={[]} />)
    expect(screen.getByText('这个任务还没有写入或删除任何文件')).toBeInTheDocument()
  })
})

describe('TaskSceneArtifacts — 展开行（L1/L2）', () => {
  it('展开可见产出工具、首末净差、内容 diff 与历史轮次；再点收起', async () => {
    const user = userEvent.setup()
    render(<TaskSceneArtifacts taskId="t3" activities={multiRoundWrites} />)

    const row = screen.getByTestId('scene-change-row')
    await user.click(within(row).getByTestId('scene-change-toggle'))

    const detail = screen.getByTestId('scene-change-detail')
    expect(within(detail).getByText('产出工具 file_write')).toBeInTheDocument()
    // 首末净差：''→'# v1\n## v2\n' 展开才算 LCS（+2 -0）
    expect(within(detail).getByText('首末净差')).toBeInTheDocument()
    expect(within(detail).getAllByText('+2').length).toBeGreaterThan(0)
    // DetailBlock diff 内容（首末全文）
    expect(within(detail).getByText('## v2')).toBeInTheDocument()

    // L2 历史轮次：两轮逐轮摘要与末轮增删
    const rounds = within(detail).getByTestId('scene-change-rounds')
    expect(rounds).toHaveTextContent('写入轮次（2）')
    expect(within(rounds).getAllByText('file_write')).toHaveLength(2)
    expect(within(rounds).getAllByText('+2').length).toBe(1)
    expect(within(rounds).getAllByText('+1').length).toBe(1)

    // 再点同一路径收起
    await user.click(within(row).getByTestId('scene-change-toggle'))
    expect(screen.queryByTestId('scene-change-detail')).toBeNull()
  })

  it('展开写入过又被删的行：状态删除、无 diff 造假，如实标注只有路径', async () => {
    const user = userEvent.setup()
    render(
      <TaskSceneArtifacts
        taskId="t4"
        activities={[writeActivity({ path: 'tmp/x.log', oldContent: 'a', newContent: 'b' }), deleteActivity('tmp/x.log')]}
      />,
    )
    const row = screen.getByTestId('scene-change-row')
    expect(within(row).getByTestId('scene-change-status')).toHaveTextContent('删除')
    await user.click(within(row).getByTestId('scene-change-toggle'))
    expect(screen.getByTestId('scene-change-detail')).toHaveTextContent('已删除（只有路径')
  })

  it('展开内容缺失（old/new 任一 null）的行：不展示 diff，给如实缺省说明', async () => {
    const user = userEvent.setup()
    render(
      <TaskSceneArtifacts
        taskId="t5"
        activities={[writeActivity({ path: 'src/nocontent.py', oldContent: null, newContent: 'print(1)' })]}
      />,
    )
    await user.click(within(screen.getByTestId('scene-change-row')).getByTestId('scene-change-toggle'))
    expect(screen.getByTestId('scene-change-detail')).toHaveTextContent('工具结果未带改前/改后全文')
  })
})

describe('TaskSceneArtifacts — 打开文件与悬浮标记', () => {
  it('注入 onOpenFile 时打开按钮走注入回调（53-57 注入分支）', async () => {
    const user = userEvent.setup()
    const onOpenFile = vi.fn()
    render(
      <TaskSceneArtifacts
        taskId="t6"
        activities={[writeActivity({ path: 'docs/relay.md', oldContent: '', newContent: 'x' })]}
        onOpenFile={onOpenFile}
      />,
    )
    await user.click(screen.getByTestId('scene-change-open'))
    expect(onOpenFile).toHaveBeenCalledWith('docs/relay.md')
    expect(mockGlobalOpen).not.toHaveBeenCalled()
  })

  it('未注入时回落全局注册表回调（57）', async () => {
    const user = userEvent.setup()
    render(
      <TaskSceneArtifacts
        taskId="t7"
        activities={[writeActivity({ path: 'docs/fallback.md', oldContent: '', newContent: 'x' })]}
      />,
    )
    await user.click(screen.getByTestId('scene-change-open'))
    expect(mockGlobalOpen).toHaveBeenCalledWith('docs/fallback.md', 't7')
  })

  it('不可内嵌类型给「可查看」角标；超 5MB 给「大文件」warning 角标', () => {
    render(
      <TaskSceneArtifacts
        taskId="t8"
        activities={[
          writeActivity({ path: 'bin/tool.bin', oldContent: 'x', newContent: 'xy', size: 1024 }),
          writeActivity({
            path: 'bin/huge.dat',
            oldContent: 'x',
            newContent: 'xy',
            size: 6 * 1024 * 1024,
          }),
        ]}
      />,
    )
    const rows = screen.getAllByTestId('scene-change-row')
    const badges = [
      ...within(rows[0] as HTMLElement).getAllByTestId('scene-badge'),
      ...within(rows[1] as HTMLElement).getAllByTestId('scene-badge'),
    ]
    expect(badges.map((b) => b.textContent)).toEqual(['可查看', '大文件'])
  })

  it('白名单内类型（.md）不给悬浮角标', () => {
    render(
      <TaskSceneArtifacts
        taskId="t9"
        activities={[writeActivity({ path: 'docs/inline.md', oldContent: '', newContent: 'ok' })]}
      />,
    )
    const mdRow = screen.getByTestId('scene-change-row')
    expect(within(mdRow).queryByTestId('scene-badge')).toBeNull()
  })
})
