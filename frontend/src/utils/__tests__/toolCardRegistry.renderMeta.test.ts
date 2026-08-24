/** @feature FP-0.2.可观测性 工具卡片条目增强（render 分支 meta 注入） @ci frontend-test */
/**
 * 功能测试：enhanceActivityWithToolConfig 的 render 声明/数据路由早退分支
 * 补齐条目增强（2026-08-19 修复）。
 *
 * 背景：双路由落地后全量工具走 applyRenderIntent/applyDataDrivenIntent 早退，
 * 该分支此前只设置 details——filePath/onOpenFile 注入与标题人性化被跳过，
 * 读文件卡片无法打开文件、条目只显示原始工具名。
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { loadRenderIntents } from '@/utils/dshRenderIntent'
import {
  enhanceActivityWithToolConfig,
  registerGlobalOpenFileCallback,
} from '@/utils/toolCardRegistry'
import type { ActivityData } from '@/types/activity'
import type { MessageToolCall } from '@/types/models'

let openFileSpy: ReturnType<typeof vi.fn> | null = null

afterEach(() => {
  loadRenderIntents([])
  if (openFileSpy) {
    registerGlobalOpenFileCallback(() => {})
    openFileSpy = null
  }
})

function makeActivity(toolName: string): ActivityData {
  return { type: 'tool_call', toolName, title: toolName, status: 'completed' } as ActivityData
}

function makeToolCall(
  tool: string,
  args: Record<string, unknown>,
  result?: Record<string, unknown>,
): MessageToolCall {
  return {
    id: 'tc1',
    tool,
    tool_args: args,
    result,
    containerTaskId: 'task-42',
    status: 'completed',
  } as unknown as MessageToolCall
}

describe('功能点：render 声明分支注入条目增强（file_read → read 卡）', () => {
  it('声明 read 卡 → 标题人性化 + subtitle=路径 + filePath/onOpenFile 注入并携带 containerTaskId', () => {
    openFileSpy = vi.fn()
    registerGlobalOpenFileCallback(openFileSpy)
    loadRenderIntents([{ name: 'file_read', render: { card: 'read' } }])

    const out = enhanceActivityWithToolConfig(
      makeActivity('file_read'),
      makeToolCall('file_read', { path: 'src/main.py' }, { file: 'src/main.py', content: 'x', total_lines: 1 }),
    )

    // 标题不再是原始工具名
    expect(out.title).toBe('读取文件')
    // 摘要行 = 文件路径（折叠态可见）
    expect(out.subtitle).toBe('src/main.py')
    // 打开文件入口已注入
    expect(out.filePath).toBe('src/main.py')
    expect(out.onOpenFile).toBeTypeOf('function')
    out.onOpenFile?.('src/main.py')
    expect(openFileSpy).toHaveBeenCalledWith('src/main.py', 'task-42')
  })

  it('声明 terminal 卡 → subtitle=命令，无文件入口（bash_execute）', () => {
    loadRenderIntents([{ name: 'bash_execute', render: { card: 'terminal' } }])
    const out = enhanceActivityWithToolConfig(
      makeActivity('bash_execute'),
      makeToolCall('bash_execute', { command: 'cargo test --lib' }, { output: 'ok', exit_code: 0 }),
    )
    expect(out.subtitle).toBe('cargo test --lib')
    expect(out.filePath).toBeUndefined()
    expect(out.onOpenFile).toBeUndefined()
  })

  it('声明 form 卡 → 无 meta（summary/filePath 均不注入）', () => {
    loadRenderIntents([{ name: 'task_submit', render: { card: 'form' } }])
    const out = enhanceActivityWithToolConfig(
      makeActivity('task_submit'),
      makeToolCall('task_submit', { goal_title: 'x' }, { task_id: 't-1' }),
    )
    expect(out.subtitle).toBeUndefined()
    expect(out.filePath).toBeUndefined()
  })
})

describe('功能点：数据路由分支注入条目增强（无声明工具）', () => {
  it('diff 形状（old/new 对 + args.file_path）→ subtitle + filePath', () => {
    const out = enhanceActivityWithToolConfig(
      makeActivity('merge'),
      makeToolCall('merge', { file_path: 'docs/a.md' }, { old_content: 'a', new_content: 'b' }),
    )
    expect(out.details?.[0]?.contentType).toBe('dsh:diff')
    expect(out.subtitle).toBe('docs/a.md')
    expect(out.filePath).toBe('docs/a.md')
    expect(out.onOpenFile).toBeTypeOf('function')
  })

  it('多文件 diffs 数组 → 有摘要无打开入口', () => {
    const out = enhanceActivityWithToolConfig(
      makeActivity('bulk_edit'),
      makeToolCall(
        'bulk_edit',
        {},
        { diffs: [
          { path: 'a.py', oldText: null, newText: 'x' },
          { path: 'b.py', oldText: null, newText: 'y' },
        ] },
      ),
    )
    expect(out.subtitle).toBeTruthy()
    expect(out.filePath).toBeUndefined()
  })
})
