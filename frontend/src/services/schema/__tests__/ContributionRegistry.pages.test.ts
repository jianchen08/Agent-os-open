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
 * - 旧查询方法(getViews/getMenus/...)是 pages 之上的薄视图；零消费的
 *   getViewsContainers/getStatusBarItems/getDockItems 已清理（统一走 getPagesBySpace）
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
    // chatActions 仍弃用（ADR widget-migration-t8-t13-t14）：chat/inline 槽无渲染方；
    // chatMessages 已恢复归一化（模式体系 §5.0）：message-style 槽由通用 webview
    // 消息卡容器承接
    expect(chat.filter((p) => p.legacyFrom === 'chatActions')).toEqual([])
    expect(chat.find((p) => p.legacyFrom === 'chatMessages')).toMatchObject({
      space: 'chat',
      slot: 'message-style',
      legacyFrom: 'chatMessages',
    })
    // 交互类(menus/commands/shortcuts)仍归一化,legacyFrom 标记真实来源
    expect(chat.map((p) => p.legacyFrom).sort()).toEqual([
      'chatMessages',
      'commands',
      'menus',
      'shortcuts',
    ])

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

  it('薄视图(getViews/getMenus/getCommands/getShortcuts/getModals)是 pages 之上的查询(getViewsContainers/getStatusBarItems 已清理)', () => {
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

    // viewsContainers/statusBarItems 薄视图已清理——经统一 API（getPagesBySpace）查询
    expect(registry.getPagesBySpace('workspace').filter((p) => p.legacyFrom === 'viewsContainers')[0].id).toBe('activity')
    expect(registry.getPagesBySpace('dock').filter((p) => p.legacyFrom === 'statusBarItems')).toHaveLength(1)
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

describe('ContributionRegistry — 跨插件页面 id 冲突（BUG-11 回归）', () => {
  let registry: ContributionRegistry

  beforeEach(() => {
    registry = new ContributionRegistry()
  })

  it('两插件声明同 id 页面 → 都注册；path 直达查询命中带 path 的声明页', () => {
    // 生产实况（BUG-11）：debug_center 先声明 id=tasks（无 path），task_service 后声明
    // id=tasks（path=/tasks）。去重若按裸 id 全局裁决，后者被静默吞掉，工作区空态
    // 「打开任务管理」按钮（openWorkspacePanelByPath('/tasks')）因此永远落空。
    registry.loadFromSchema(
      makeSchema({
        plugin_contributes: [
          {
            plugin_id: 'debug_center',
            contributes: {
              pages: [{ id: 'tasks', title: '任务', space: 'debug_center', widget: 'debug_tasks' }],
            },
          },
          {
            plugin_id: 'task_service',
            contributes: {
              pages: [
                {
                  id: 'tasks',
                  title: '任务管理',
                  space: 'workspace',
                  slot: 'tab',
                  path: '/tasks',
                  widget: 'pipeline_manager',
                },
              ],
            },
          },
        ],
      }),
    )

    const byPath = registry.getPages().find((p) => p.path === '/tasks')
    expect(byPath).toMatchObject({ pluginId: 'task_service', widget: 'pipeline_manager' })
    // debug_center 的同名页不受影响
    expect(registry.getPagesBySpace('debug_center').map((p) => p.id)).toContain('tasks')
    // getPage 裸 id 查询保持先注册者胜出（既有可观察行为不变）
    expect(registry.getPage('tasks')?.pluginId).toBe('debug_center')
  })
})

describe('ContributionRegistry — 未知贡献点 key 忽略（导航页垃圾卡片回归）', () => {
  let registry: ContributionRegistry

  beforeEach(() => {
    registry = new ContributionRegistry()
  })

  it('非页面贡献 key（renderers/thread_fields）不归一化为页面', () => {
    // 生产实况：dsh_adapter.contributes.renderers、isolation/workspace_lifecycle
    // .contributes.thread_fields 是非页面声明，兜底归一化会在工作区导航页产生
    // 「dsh_adapter:renderers:{"card":"read"...」式垃圾卡片（无 id/title 条目
    // 经 synthesizeId 兜底成 JSON 截断串）。
    registry.registerFromSchema({
      plugin_contributes: [
        {
          plugin_id: 'dsh_adapter',
          contributes: {
            renderers: [{ card: 'read', tool: 'dsh_read' }],
            pages: [{ id: 'real_page', title: '真实页面', space: 'workspace' }],
          },
        },
        {
          plugin_id: 'isolation',
          contributes: {
            thread_fields: [{ id: 'main', description: '主会话执行环境隔离（容器/宿主）' }],
          },
        },
      ],
    })

    const pages = registry.getPages()
    expect(pages.map((p) => p.id)).toEqual(['real_page'])
    expect(pages.every((p) => !p.id.includes('renderers'))).toBe(true)
    expect(pages.every((p) => !p.id.includes('thread_fields'))).toBe(true)
  })

  it('已知旧贡献点 key 与未知 key 混合时仅旧 key 正常归一化', () => {
    registry.registerFromSchema({
      plugin_contributes: [
        {
          plugin_id: 'p',
          contributes: {
            viewsContainers: [{ id: 'vc1', title: '侧栏入口', icon: '⚡' }],
            some_future_key: [{ id: 'x' }],
          },
        },
      ],
    })

    const activityBar = registry.getPagesBySpace('workspace').filter((p) => p.slot === 'activity-bar')
    expect(activityBar.map((p) => p.id)).toEqual(['vc1'])
    // 未知 key 无兜底页面产生
    expect(registry.getPages().every((p) => p.id !== 'x')).toBe(true)
  })
})
