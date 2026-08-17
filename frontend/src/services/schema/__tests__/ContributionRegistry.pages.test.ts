/**
 * ContributionRegistry — pages 归一化注册测试
 *
 * 覆盖阶段2(前端部分A)+ 方向变更(直接归一化):
 * - contributes.pages[] 声明 → getPages / getPagesBySpace / getPage / getPluginPages
 * - 字段化 page(detachable/schema/layout/widget/props/writable 等)原样保留
 * - 旧贡献点 key(viewsContainers/views/statusBarItems/dockItems/floating/
 *   workspaceTabs/chatMessages/chatInteractions/chatActions/menus/commands/
 *   shortcuts/modal/settingsPanels/widgets)在注册时直接归一化为
 *   PageDeclaration(带 legacyFrom 来源标记),无第二套存储
 * - 旧查询方法(getViewsContainers/getStatusBarItems/getMenus/...)是 pages 之上的薄视图,仍可用
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

describe('ContributionRegistry — contributes.pages 声明注册', () => {
  let registry: ContributionRegistry

  beforeEach(() => {
    registry = new ContributionRegistry()
  })

  it('registerFromSchema 收到含 contributes.pages 的 schema → getPages() 返回该 page', () => {
    registry.registerFromSchema({
      plugin_contributes: [
        {
          plugin_id: 'my-plugin',
          plugin_name: '我的插件',
          contributes: {
            pages: [{ id: 'my_page', title: '页面', icon: 'bot', space: 'workspace' }],
          },
        },
      ],
    })

    const pages = registry.getPages()
    expect(pages).toHaveLength(1)
    expect(pages[0].id).toBe('my_page')
    expect(pages[0].title).toBe('页面')
    expect(pages[0].icon).toBe('bot')
    expect(pages[0].space).toBe('workspace')
    expect(pages[0].pluginId).toBe('my-plugin')
    expect(pages[0].legacyFrom).toBeUndefined()
  })

  it('getPagesBySpace(space) 按 space 过滤', () => {
    registry.loadFromSchema(
      makeSchema({
        plugin_contributes: [
          {
            plugin_id: 'p',
            contributes: {
              pages: [
                { id: 'a', space: 'workspace' },
                { id: 'b', space: 'settings' },
                { id: 'c', space: 'workspace' },
              ],
            },
          },
        ],
      }),
    )

    const workspace = registry.getPagesBySpace('workspace')
    expect(workspace.map((p) => p.id)).toEqual(['a', 'c'])
    expect(registry.getPagesBySpace('chat')).toEqual([])
    expect(registry.getPagesBySpace('floating')).toEqual([])
  })

  it('getPage(id) 查找正确', () => {
    registry.loadFromSchema(
      makeSchema({
        plugin_contributes: [
          {
            plugin_id: 'p',
            contributes: {
              pages: [
                { id: 'alpha', space: 'workspace', title: 'Alpha' },
                { id: 'beta', space: 'settings', title: 'Beta' },
              ],
            },
          },
        ],
      }),
    )

    expect(registry.getPage('beta')?.title).toBe('Beta')
    expect(registry.getPage('alpha')?.space).toBe('workspace')
    expect(registry.getPage('missing')).toBeUndefined()
  })

  it('含 detachable/schema/layout 等字段的 page 原样保留', () => {
    const detachable = {
      popout: true,
      childWindow: false,
      persist: true,
      defaultSize: { w: 640, h: 480 },
      alwaysOnTop: true,
    }
    const schema = { fields: [{ name: 'name', type: 'string', label: '名称', required: true }] }
    const layout = [{ tab: '基础', fields: ['name'] }]

    registry.loadFromSchema(
      makeSchema({
        plugin_contributes: [
          {
            plugin_id: 'p',
            contributes: {
              pages: [
                {
                  id: 'rich',
                  title: '富页面',
                  space: 'workspace',
                  slot: 'tab',
                  path: '/rich',
                  order: 30,
                  when: 'user.isAdmin',
                  datasourceUri: '/api/v1/p/rich',
                  schema,
                  layout,
                  widget: 'webview',
                  props: { htmlPath: '/editor' },
                  writable: true,
                  detachable,
                },
              ],
            },
          },
        ],
      }),
    )

    const page = registry.getPage('rich')
    expect(page).toBeDefined()
    expect(page?.detachable).toEqual(detachable)
    expect(page?.schema).toEqual(schema)
    expect(page?.layout).toEqual(layout)
    expect(page?.widget).toBe('webview')
    expect(page?.props).toEqual({ htmlPath: '/editor' })
    expect(page?.writable).toBe(true)
    expect(page?.when).toBe('user.isAdmin')
    expect(page?.datasourceUri).toBe('/api/v1/p/rich')
    expect(page?.path).toBe('/rich')
    expect(page?.order).toBe(30)
    expect(page?.slot).toBe('tab')
  })

  it('getPluginPages(pluginId) 只返回该插件的页面', () => {
    registry.loadFromSchema(
      makeSchema({
        plugin_contributes: [
          { plugin_id: 'a', contributes: { pages: [{ id: 'pa', space: 'workspace' }] } },
          { plugin_id: 'b', contributes: { pages: [{ id: 'pb', space: 'settings' }] } },
        ],
      }),
    )

    const pages = registry.getPluginPages('a')
    expect(pages.map((p) => p.id)).toEqual(['pa'])
    expect(registry.getPluginPages('nope')).toEqual([])
  })
})

describe('ContributionRegistry — 旧贡献点直接归一化为 pages', () => {
  let registry: ContributionRegistry

  beforeEach(() => {
    registry = new ContributionRegistry()
  })

  it('viewsContainers → workspace/activity-bar 页(带 legacyFrom 来源标记)', () => {
    registry.loadFromSchema(
      makeSchema({
        plugin_contributes: [
          {
            plugin_id: 'legacy-plug',
            contributes: {
              viewsContainers: [{ id: 'activity', title: '侧栏', icon: 'bot', path: '/activity' }],
            },
          },
        ],
      }),
    )

    const page = registry.getPagesBySpace('workspace')[0]
    expect(page).toBeDefined()
    expect(page.legacyFrom).toBe('viewsContainers')
    expect(page.slot).toBe('activity-bar')
    expect(page.id).toBe('activity')
    expect(page.title).toBe('侧栏')
    expect(page.icon).toBe('bot')
    expect(page.path).toBe('/activity')
    expect(page.pluginId).toBe('legacy-plug')
  })

  it('views → workspace/tab 页,containerId/widget 透传', () => {
    registry.loadFromSchema(
      makeSchema({
        plugin_contributes: [
          {
            plugin_id: 'p',
            contributes: {
              views: [{ id: 'view1', title: '视图一', containerId: 'activity', widget: 'review_document' }],
            },
          },
        ],
      }),
    )

    const page = registry.getPage('view1')
    expect(page?.space).toBe('workspace')
    expect(page?.slot).toBe('tab')
    expect(page?.widget).toBe('review_document')
    expect(page?.containerId).toBe('activity')
    expect(page?.legacyFrom).toBe('views')
  })

  it('statusBarItems/dockItems/floating/workspaceTabs 归一化到对应 space/slot', () => {
    registry.loadFromSchema(
      makeSchema({
        plugin_contributes: [
          {
            plugin_id: 'p',
            contributes: {
              statusBarItems: [{ id: 'sb1', title: '状态' }],
              dockItems: [{ id: 'dock1', title: 'Dock' }],
              floating: [{ id: 'flt1', title: '浮窗' }],
              workspaceTabs: [{ id: 'tab1', title: '工作页' }],
            },
          },
        ],
      }),
    )

    const dock = registry.getPagesBySpace('dock')
    expect(dock).toHaveLength(2)
    expect(dock.find((p) => p.id === 'sb1')).toMatchObject({ space: 'dock', slot: 'status', legacyFrom: 'statusBarItems' })
    expect(dock.find((p) => p.id === 'dock1')).toMatchObject({ space: 'dock', slot: 'item', legacyFrom: 'dockItems' })
    expect(registry.getPage('flt1')).toMatchObject({ space: 'floating', slot: 'panel', legacyFrom: 'floating' })
    // workspaceTabs 已弃用（ADR widget-migration-t8-t13-t14）：不再归一化
    expect(registry.getPage('tab1')).toBeUndefined()
  })

  it('chat 系列已弃用；menus/commands/shortcuts/modal 归一化且旧字段透传', () => {
    registry.loadFromSchema(
      makeSchema({
        plugin_contributes: [
          {
            plugin_id: 'p',
            contributes: {
              chatActions: [{ id: 'ca1', title: '输入区动作' }],
              chatMessages: [{ id: 'cm1', title: '消息样式' }],
              menus: [{ id: 'm1', location: 'workspace/context', title: 'M1', command: 'c1', when: 'resource.isFile' }],
              commands: [{ id: 'cmd1', title: '命令', category: '工具' }],
              shortcuts: [{ command: 'editor.save', key: 'Ctrl+S', when: 'workspace.focus' }],
              modal: [{ id: 'modal1', title: '弹窗', trigger: 'on_command:cmd1', widget: 'approval_card', props: { mode: 'strict' } }],
            },
          },
        ],
      }),
    )

    const chat = registry.getPagesBySpace('chat')
    // chat 系列（chatActions/chatMessages）已弃用（ADR widget-migration-t8-t13-t14）：
    // chat/inline 槽无渲染方，场景由工具卡协议（ui.chat_card / render）覆盖
    expect(chat.filter((p) => p.legacyFrom === 'chatActions' || p.legacyFrom === 'chatMessages')).toEqual([])
    // 交互类(menus/commands/shortcuts)仍归一化,legacyFrom 标记真实来源
    expect(chat.map((p) => p.legacyFrom).sort()).toEqual(['commands', 'menus', 'shortcuts'])

    const menu = registry.getPage('m1')
    expect(menu).toMatchObject({ space: 'chat', slot: 'inline', legacyFrom: 'menus', location: 'workspace/context', command: 'c1', when: 'resource.isFile' })

    const cmd = registry.getPage('cmd1')
    expect(cmd).toMatchObject({ legacyFrom: 'commands', category: '工具', title: '命令' })

    // shortcuts 无显式 id → 由 command 合成稳定 id
    const sc = registry.getPages().find((p) => p.legacyFrom === 'shortcuts')
    expect(sc).toBeDefined()
    expect(sc?.key).toBe('Ctrl+S')
    expect(sc?.command).toBe('editor.save')
    expect(sc?.pluginId).toBe('p')

    const modal = registry.getPage('modal1')
    expect(modal).toMatchObject({ legacyFrom: 'modal', trigger: 'on_command:cmd1', widget: 'approval_card', props: { mode: 'strict' } })
  })

  it('plugin_configs 的 config_files 归一化为 settings/nav 页(datasourceUri=路径)', () => {
    registry.loadFromSchema(
      makeSchema({
        plugin_configs: [
          {
            plugin_id: 'connectors',
            plugin_name: '连接器',
            config_files: [
              { id: 'godot', path: 'config/external_tools/godot.yaml', label: 'Godot' },
              { id: 'vscode', path: 'config/external_tools/vscode.yaml', label: 'VSCode' },
            ],
          },
        ],
      }),
    )

    const settingsPages = registry.getPagesBySpace('settings')
    expect(settingsPages).toHaveLength(2)
    const godot = settingsPages.find((p) => p.title === 'Godot')
    expect(godot).toMatchObject({
      space: 'settings',
      slot: 'nav',
      datasourceUri: 'config/external_tools/godot.yaml',
      pluginId: 'connectors',
      legacyFrom: 'settingsPanels',
    })
  })

  it('旧查询方法是 pages 之上的薄视图(getViewsContainers/getStatusBarItems/getMenus/getCommands/getShortcuts/getModals 仍工作)', () => {
    registry.loadFromSchema(
      makeSchema({
        plugin_configs: [
          { plugin_id: 'cfg', plugin_name: '配置', config_files: [{ id: 'f1', path: 'p1.yaml', label: 'P1' }] },
        ],
        plugin_contributes: [
          {
            plugin_id: 'p',
            contributes: {
              viewsContainers: [{ id: 'activity', title: '侧栏' }],
              statusBarItems: [{ id: 'sb', title: '状态' }],
              pages: [{ id: 'pg', space: 'workspace' }],
              menus: [{ id: 'm1', location: 'workspace/context', title: 'M1', command: 'c1' }],
              commands: [{ id: 'cmd1', title: '命令' }],
              shortcuts: [{ command: 's1', key: 'Ctrl+K' }],
              modal: [{ id: 'modal1', trigger: 'on_command:cmd1', widget: 'w1' }],
            },
          },
        ],
      }),
    )

    expect(registry.getViewsContainers()).toHaveLength(1)
    expect(registry.getViewsContainers()[0].id).toBe('activity')
    expect(registry.getStatusBarItems()).toHaveLength(1)
    expect(registry.getMenus('workspace/context')).toHaveLength(1)
    expect(registry.getCommands()[0].id).toBe('cmd1')
    expect(registry.getShortcuts()[0].key).toBe('Ctrl+K')
    expect(registry.getModals()).toHaveLength(1)
    expect(registry.findModalByTrigger('on_command:cmd1')?.id).toBe('modal1')
    // getByType 薄视图:pages = 声明页;旧 key = 归一化页
    expect(registry.getByType('pages').map((p) => p.id)).toEqual(['pg'])
    expect(registry.getByType('menus')).toHaveLength(1)
    // 配置面板注册表(plugin_configs)不受影响
    expect(registry.getPluginConfigFiles('cfg')).toHaveLength(1)
  })

  it('再次 loadFromSchema 清空后重新归一化(无幽灵页)', () => {
    const withPages = makeSchema({
      plugin_contributes: [
        { plugin_id: 'p', contributes: { pages: [{ id: 'pg1', space: 'workspace' }], statusBarItems: [{ id: 'sb1', title: 'S' }] } },
      ],
    })
    registry.loadFromSchema(withPages)
    expect(registry.getPages()).toHaveLength(2)

    registry.loadFromSchema(makeSchema())
    expect(registry.getPages()).toEqual([])
    expect(registry.getPagesBySpace('workspace')).toEqual([])
    expect(registry.getPagesBySpace('dock')).toEqual([])
  })
})
