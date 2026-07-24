/**
 * ContributionRegistry — P5 contributes 插槽测试（ADR §3.4 档位二）
 *
 * menus / commands / shortcuts / modal 四类贡献点的解析与查询。
 * 数据源：/api/v1/schema 聚合的插件 manifest contributes.*（内核已透传）。
 */

import { describe, it, expect, beforeEach } from 'vitest'
import { ContributionRegistry } from '@/services/schema/ContributionRegistry'
import type { SchemaResponse } from '@/services/api/schema'

function makeSchema(overrides: Partial<SchemaResponse> = {}): SchemaResponse {
  return {
    agents: [],
    pipelines: [],
    tools: [],
    routes: {},
    ...overrides,
  } as SchemaResponse
}

describe('ContributionRegistry — contributes.menus 解析', () => {
  let registry: ContributionRegistry

  beforeEach(() => {
    registry = new ContributionRegistry()
  })

  it('从 modules.contributes.menus 提取右键菜单项', () => {
    registry.loadFromSchema(
      makeSchema({
        modules: [
          {
            module_id: 'editor',
            contributes: {
              menus: [
                {
                  id: 'editor.gotoDef',
                  location: 'workspace/context',
                  title: '跳转到定义',
                  command: 'editor.gotoDef',
                  when: 'resource.isFile',
                },
              ],
            },
          },
        ],
      } as unknown as SchemaResponse),
    )

    const menus = registry.getMenus('workspace/context')
    expect(menus).toHaveLength(1)
    expect(menus[0].id).toBe('editor.gotoDef')
    expect(menus[0].title).toBe('跳转到定义')
    expect(menus[0].pluginId).toBe('editor')
  })

  it('按 location 过滤菜单项', () => {
    registry.loadFromSchema(
      makeSchema({
        modules: [
          {
            module_id: 'p',
            contributes: {
              menus: [
                { id: 'm1', location: 'workspace/context', title: 'M1', command: 'c1' },
                { id: 'm2', location: 'chat/context', title: 'M2', command: 'c2' },
              ],
            },
          },
        ],
      } as unknown as SchemaResponse),
    )

    expect(registry.getMenus('workspace/context')).toHaveLength(1)
    expect(registry.getMenus('chat/context')).toHaveLength(1)
    expect(registry.getMenus('other/context')).toEqual([])
  })

  it('无 location 入参时返回全部菜单', () => {
    registry.loadFromSchema(
      makeSchema({
        modules: [
          {
            module_id: 'p',
            contributes: {
              menus: [
                { id: 'm1', location: 'workspace/context', title: 'M1', command: 'c1' },
                { id: 'm2', location: 'chat/context', title: 'M2', command: 'c2' },
              ],
            },
          },
        ],
      } as unknown as SchemaResponse),
    )

    expect(registry.getMenus()).toHaveLength(2)
  })
})

describe('ContributionRegistry — contributes.commands 解析', () => {
  let registry: ContributionRegistry

  beforeEach(() => {
    registry = new ContributionRegistry()
  })

  it('从 modules.contributes.commands 提取命令', () => {
    registry.loadFromSchema(
      makeSchema({
        modules: [
          {
            module_id: 'cost',
            contributes: {
              commands: [
                { id: 'cost.showReport', title: '显示成本报告', category: '成本', icon: 'chart' },
              ],
            },
          },
        ],
      } as unknown as SchemaResponse),
    )

    const commands = registry.getCommands()
    expect(commands).toHaveLength(1)
    expect(commands[0].id).toBe('cost.showReport')
    expect(commands[0].title).toBe('显示成本报告')
    expect(commands[0].category).toBe('成本')
    expect(commands[0].pluginId).toBe('cost')
  })

  it('多插件 commands 聚合', () => {
    registry.loadFromSchema(
      makeSchema({
        modules: [
          {
            module_id: 'a',
            contributes: { commands: [{ id: 'a.cmd', title: 'A', category: 'X' }] },
          },
          {
            module_id: 'b',
            contributes: { commands: [{ id: 'b.cmd', title: 'B', category: 'Y' }] },
          },
        ],
      } as unknown as SchemaResponse),
    )

    const commands = registry.getCommands()
    expect(commands.map((c) => c.id)).toEqual(['a.cmd', 'b.cmd'])
  })
})

describe('ContributionRegistry — contributes.shortcuts 解析', () => {
  let registry: ContributionRegistry

  beforeEach(() => {
    registry = new ContributionRegistry()
  })

  it('从 modules.contributes.shortcuts 提取快捷键绑定', () => {
    registry.loadFromSchema(
      makeSchema({
        modules: [
          {
            module_id: 'editor',
            contributes: {
              shortcuts: [
                { command: 'editor.save', key: 'Ctrl+S', when: 'workspace.focus' },
              ],
            },
          },
        ],
      } as unknown as SchemaResponse),
    )

    const shortcuts = registry.getShortcuts()
    expect(shortcuts).toHaveLength(1)
    expect(shortcuts[0].command).toBe('editor.save')
    expect(shortcuts[0].key).toBe('Ctrl+S')
    expect(shortcuts[0].when).toBe('workspace.focus')
    expect(shortcuts[0].pluginId).toBe('editor')
  })
})

describe('ContributionRegistry — contributes.modal 解析', () => {
  let registry: ContributionRegistry

  beforeEach(() => {
    registry = new ContributionRegistry()
  })

  it('从 modules.contributes.modal 提取模态弹窗', () => {
    registry.loadFromSchema(
      makeSchema({
        modules: [
          {
            module_id: 'review',
            contributes: {
              modal: [
                {
                  id: 'review.approve',
                  title: '审批确认',
                  trigger: 'on_command:review.approve',
                  widget: 'approval_card',
                  props: { mode: 'strict' },
                },
              ],
            },
          },
        ],
      } as unknown as SchemaResponse),
    )

    const modals = registry.getModals()
    expect(modals).toHaveLength(1)
    expect(modals[0].id).toBe('review.approve')
    expect(modals[0].widget).toBe('approval_card')
    expect(modals[0].trigger).toBe('on_command:review.approve')
    expect(modals[0].pluginId).toBe('review')
  })

  it('按 trigger 查找模态弹窗', () => {
    registry.loadFromSchema(
      makeSchema({
        modules: [
          {
            module_id: 'p',
            contributes: {
              modal: [
                { id: 'm1', title: 'M1', trigger: 'on_command:cmd.a', widget: 'w1' },
                { id: 'm2', title: 'M2', trigger: 'on_command:cmd.b', widget: 'w2' },
              ],
            },
          },
        ],
      } as unknown as SchemaResponse),
    )

    const modal = registry.findModalByTrigger('on_command:cmd.a')
    expect(modal?.id).toBe('m1')
  })

  it('trigger 未命中返回 undefined', () => {
    registry.loadFromSchema(
      makeSchema({
        modules: [
          {
            module_id: 'p',
            contributes: { modal: [{ id: 'm1', title: 'M1', trigger: 'on_command:cmd.a', widget: 'w1' }] },
          },
        ],
      } as unknown as SchemaResponse),
    )

    expect(registry.findModalByTrigger('on_command:nope')).toBeUndefined()
  })
})

describe('ContributionRegistry — 重新加载清空旧插槽', () => {
  it('再次 loadFromSchema 覆盖旧的 contributes（避免幽灵菜单）', () => {
    const registry = new ContributionRegistry()
    const withMenus = makeSchema({
      modules: [
        { module_id: 'p', contributes: { menus: [{ id: 'm1', location: 'workspace/context', title: 'M1', command: 'c1' }] } },
      ],
    } as unknown as SchemaResponse)
    registry.loadFromSchema(withMenus)
    expect(registry.getMenus()).toHaveLength(1)

    registry.loadFromSchema(makeSchema())
    expect(registry.getMenus()).toEqual([])
    expect(registry.getCommands()).toEqual([])
    expect(registry.getShortcuts()).toEqual([])
    expect(registry.getModals()).toEqual([])
  })
})
