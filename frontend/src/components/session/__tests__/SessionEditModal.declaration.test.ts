/**
 * SessionEditModal 声明驱动翻译层测试
 *
 * 契约：字段来自不同插件，值→metadata 存储键（x_metadata_key）、值→
 * execution_context 生效路径（x_execution_path）、跨字段值守卫（x_guard）
 * 全部由声明表达，翻译层不感知任何具体字段语义。
 */
import { describe, expect, it } from 'vitest'
import { applyGuards, toFormOptions } from '@/components/session/SessionEditModal'
import type { ThreadField } from '@/services/api/session'

// 两个不同插件贡献的字段声明（形状取自真实 plugin.json）
const workspaceFields: ThreadField[] = [
  {
    name: 'workspace',
    type: 'string',
    label: '工作空间',
    x_metadata_key: 'workspace',
    x_execution_path: 'workspace.source_path',
  },
  {
    name: 'workspaceMode',
    type: 'select',
    label: '工作空间拓扑',
    x_metadata_key: 'workspace_mode',
    x_execution_path: 'workspace.mode',
    x_guard: { requires: 'workspace', on_empty: 'plain' },
    options: [
      { label: 'plain', value: 'plain' },
      { label: 'worktree', value: 'worktree' },
    ],
  },
]

const isolationFields: ThreadField[] = [
  {
    name: 'isolationMode',
    type: 'select',
    label: '隔离模式',
    x_metadata_key: 'isolation_mode',
    x_execution_path: 'isolation.level',
    options: [
      { label: '非隔离', value: 'non_isolated' },
      { label: '隔离（容器）', value: 'isolated' },
    ],
  },
]

const allFields = [...workspaceFields, ...isolationFields]

describe('applyGuards：声明化跨字段值守卫', () => {
  it('依赖源已填 → 保留用户选择的 worktree', () => {
    const out = applyGuards(allFields, {
      workspace: 'D:/proj/demo',
      workspaceMode: 'worktree',
      isolationMode: 'non_isolated',
    })
    expect(out.workspaceMode).toBe('worktree')
  })

  it('依赖源为空/空白 → 回退声明值 plain（含初始值兜底路径）', () => {
    const empty = applyGuards(allFields, { workspace: '', workspaceMode: 'worktree' })
    expect(empty.workspaceMode).toBe('plain')

    const blank = applyGuards(allFields, { workspace: '   ', workspaceMode: 'worktree' })
    expect(blank.workspaceMode).toBe('plain')

    const missing = applyGuards(allFields, { workspaceMode: 'worktree' })
    expect(missing.workspaceMode).toBe('plain')
  })

  it('无守卫声明字段不受影响', () => {
    const out = applyGuards(allFields, { workspace: '', isolationMode: 'isolated' })
    expect(out.isolationMode).toBe('isolated')
  })
})

describe('toFormOptions：声明名值包 → metadata 形状 + execution_context', () => {
  it('不同插件字段按各自声明的存储键与生效路径收敛，互不干扰', () => {
    const out = toFormOptions(allFields, {
      workspace: 'D:/proj/demo',
      workspaceMode: 'worktree',
      isolationMode: 'isolated',
    })
    expect(out.fieldMetadata).toEqual({
      workspace: 'D:/proj/demo',
      workspace_mode: 'worktree',
      isolation_mode: 'isolated',
    })
    expect(out.executionContext).toEqual({
      workspace: { source_path: 'D:/proj/demo', mode: 'worktree' },
      isolation: { level: 'isolated' },
    })
  })

  it('空值字段不写入任一产物', () => {
    const out = toFormOptions(allFields, { workspace: '', workspaceMode: 'plain' })
    expect(out.fieldMetadata).toEqual({ workspace_mode: 'plain' })
    expect(out.executionContext).toEqual({ workspace: { mode: 'plain' } })
  })

  it('全部字段为空 → executionContext 缺省', () => {
    const out = toFormOptions(allFields, {})
    expect(out.fieldMetadata).toEqual({})
    expect(out.executionContext).toBeUndefined()
  })
})
