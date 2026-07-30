/**
 * CommandDispatcher 测试（P5 a/b/c/d 核心）
 *
 * 命令是 P5 四类插槽的统一出口：menu 点击 / 命令面板 / 快捷键 / modal trigger
 * 都经 CommandDispatcher 触发，由它路由到内核（插件 capability）或打开 modal。
 *
 * 不 Mock 被测逻辑（CommandDispatcher 本体），仅 Mock 内核调用（transport）。
 */

import { describe, it, expect, beforeEach, vi } from 'vitest'
import { CommandDispatcher } from '@/services/schema/commandDispatcher'
import { ContributionRegistry } from '@/services/schema/ContributionRegistry'
import { useContextKeys } from '@/stores/contextKeysStore'

describe('CommandDispatcher — 命令执行', () => {
  let dispatcher: CommandDispatcher
  let registry: ContributionRegistry

  beforeEach(() => {
    registry = new ContributionRegistry()
    dispatcher = new CommandDispatcher(registry)
    useContextKeys.getState().reset()
  })

  it('executeCommand 调用注入的 transport（路由到内核/插件）', async () => {
    const transport = vi.fn().mockResolvedValue({ ok: true })
    dispatcher.setTransport(transport)

    await dispatcher.executeCommand('cost.showReport', { arg: 1 })

    expect(transport).toHaveBeenCalledWith('cost.showReport', { arg: 1 })
  })

  it('未注册 transport 时 executeCommand 不抛错（静默降级，记日志）', async () => {
    await expect(dispatcher.executeCommand('any.cmd')).resolves.toBeUndefined()
  })

  it('命令不存在 contributes.commands 也能执行（transport 自行决定）', async () => {
    const transport = vi.fn().mockResolvedValue(undefined)
    dispatcher.setTransport(transport)
    await dispatcher.executeCommand('dynamic.cmd')
    expect(transport).toHaveBeenCalledWith('dynamic.cmd', undefined)
  })
})

describe('CommandDispatcher — modal trigger（P5-d）', () => {
  let dispatcher: CommandDispatcher
  let registry: ContributionRegistry

  beforeEach(() => {
    registry = new ContributionRegistry()
    dispatcher = new CommandDispatcher(registry)
    useContextKeys.getState().reset()
  })

  it('命令触发 modal：声明 trigger=on_command:xxx 的 modal 被打开', async () => {
    // 模拟插件声明：cost.showReport 命令 + 对应 modal
    ;(registry as unknown as { loadFromSchema: (s: unknown) => void }).loadFromSchema({
      plugin_contributes: [
        {
          plugin_id: 'cost',
          contributes: {
            commands: [{ id: 'cost.showReport', title: '显示成本报告', category: '成本' }],
            modal: [
              {
                id: 'cost.reportModal',
                title: '成本报告',
                trigger: 'on_command:cost.showReport',
                widget: 'chart',
                props: { metric: 'tokens' },
              },
            ],
          },
        },
      ],
    })

    const onModalOpen = vi.fn()
    dispatcher.onModalOpen(onModalOpen)
    dispatcher.setTransport(vi.fn().mockResolvedValue(undefined))

    await dispatcher.executeCommand('cost.showReport')

    expect(onModalOpen).toHaveBeenCalledTimes(1)
    const opened = onModalOpen.mock.calls[0][0]
    expect(opened.id).toBe('cost.reportModal')
    expect(opened.widget).toBe('chart')
  })

  it('无 modal 绑定的命令不触发 modal 打开', async () => {
    ;(registry as unknown as { loadFromSchema: (s: unknown) => void }).loadFromSchema({
      plugin_contributes: [
        { plugin_id: 'p', contributes: { commands: [{ id: 'p.cmd', title: 'P' }] } },
      ],
    })
    const onModalOpen = vi.fn()
    dispatcher.onModalOpen(onModalOpen)
    dispatcher.setTransport(vi.fn().mockResolvedValue(undefined))

    await dispatcher.executeCommand('p.cmd')
    expect(onModalOpen).not.toHaveBeenCalled()
  })
})

describe('CommandDispatcher — 命令面板搜索（P5-b）', () => {
  let dispatcher: CommandDispatcher
  let registry: ContributionRegistry

  beforeEach(() => {
    registry = new ContributionRegistry()
    dispatcher = new CommandDispatcher(registry)
    useContextKeys.getState().reset()
  })

  it('searchCommands 按标题模糊匹配', () => {
    ;(registry as unknown as { loadFromSchema: (s: unknown) => void }).loadFromSchema({
      plugin_contributes: [
        {
          plugin_id: 'cost',
          contributes: {
            commands: [
              { id: 'cost.showReport', title: '显示成本报告', category: '成本' },
              { id: 'cost.export', title: '导出CSV', category: '成本' },
            ],
          },
        },
        {
          plugin_id: 'editor',
          contributes: { commands: [{ id: 'editor.save', title: '保存文件', category: '编辑' }] },
        },
      ],
    })

    const results = dispatcher.searchCommands('成本报告')
    expect(results).toHaveLength(1)
    expect(results[0].id).toBe('cost.showReport')
  })

  it('空查询返回全部命令', () => {
    ;(registry as unknown as { loadFromSchema: (s: unknown) => void }).loadFromSchema({
      plugin_contributes: [
        { plugin_id: 'p', contributes: { commands: [{ id: 'p.a', title: 'A' }, { id: 'p.b', title: 'B' }] } },
      ],
    })
    expect(dispatcher.searchCommands('')).toHaveLength(2)
  })

  it('搜索匹配 category', () => {
    ;(registry as unknown as { loadFromSchema: (s: unknown) => void }).loadFromSchema({
      plugin_contributes: [
        {
          plugin_id: 'p',
          contributes: {
            commands: [
              { id: 'p.a', title: 'AAA', category: '编辑' },
              { id: 'p.b', title: 'BBB', category: '编辑' },
            ],
          },
        },
      ],
    })
    const results = dispatcher.searchCommands('编辑')
    expect(results).toHaveLength(2)
  })

  it('搜索大小写不敏感', () => {
    ;(registry as unknown as { loadFromSchema: (s: unknown) => void }).loadFromSchema({
      plugin_contributes: [
        { plugin_id: 'p', contributes: { commands: [{ id: 'p.csv', title: 'Export CSV' }] } },
      ],
    })
    expect(dispatcher.searchCommands('csv')).toHaveLength(1)
    expect(dispatcher.searchCommands('EXPORT')).toHaveLength(1)
  })
})

describe('CommandDispatcher — when 过滤', () => {
  let dispatcher: CommandDispatcher
  let registry: ContributionRegistry

  beforeEach(() => {
    registry = new ContributionRegistry()
    dispatcher = new CommandDispatcher(registry)
    useContextKeys.getState().reset()
  })

  it('getVisibleCommands 过滤掉 when 失配的命令', () => {
    ;(registry as unknown as { loadFromSchema: (s: unknown) => void }).loadFromSchema({
      plugin_contributes: [
        {
          plugin_id: 'p',
          contributes: {
            commands: [
              { id: 'p.always', title: '总是可见' },
              { id: 'p.runOnly', title: '运行时', when: 'pipeline.running' },
            ],
          },
        },
      ],
    })

    // 默认 pipeline 未运行 → runOnly 被过滤
    const visible = dispatcher.getVisibleCommands()
    expect(visible.map((c) => c.id)).toEqual(['p.always'])

    // 启动后都可见
    useContextKeys.getState().setPipelineRunning(true)
    const visible2 = dispatcher.getVisibleCommands()
    expect(visible2.map((c) => c.id).sort()).toEqual(['p.always', 'p.runOnly'])
  })

  it('getVisibleMenus 按 location + when 过滤', () => {
    ;(registry as unknown as { loadFromSchema: (s: unknown) => void }).loadFromSchema({
      plugin_contributes: [
        {
          plugin_id: 'p',
          contributes: {
            menus: [
              { id: 'p.m1', location: 'workspace/context', title: 'M1', command: 'c1' },
              { id: 'p.m2', location: 'workspace/context', title: 'M2', command: 'c2', when: 'resource.isFile' },
            ],
          },
        },
      ],
    })

    // resource.isFile 默认 false → m2 被过滤
    const visible = dispatcher.getVisibleMenus('workspace/context')
    expect(visible.map((m) => m.id)).toEqual(['p.m1'])

    useContextKeys.getState().setResource({ isFile: true, extname: '.py' })
    const visible2 = dispatcher.getVisibleMenus('workspace/context')
    expect(visible2.map((m) => m.id).sort()).toEqual(['p.m1', 'p.m2'])
  })
})
