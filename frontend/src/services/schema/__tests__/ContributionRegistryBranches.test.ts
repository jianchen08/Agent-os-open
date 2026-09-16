/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * ContributionRegistry 分支补测：既有测试用的是「镜像类/直接 register」路径，
 * 本文件走真实 ContributionRegistry + registerFromSchema 归一化链路，补齐
 * 未覆盖分支——未知贡献 key 兜底、合成 id 各分支、去重与 order 排序、
 * 注销索引重建、getByType 双面语义、clear 全量复位。
 */
import { beforeEach, describe, expect, it } from 'vitest'
import { ContributionRegistry } from '../ContributionRegistry'

let registry: ContributionRegistry

beforeEach(() => {
  registry = new ContributionRegistry()
})

describe('ContributionRegistry — registerFromSchema 初始化与缺省', () => {
  it('未调用 registerFromSchema 时 isInitialized 为 false', () => {
    expect(registry.isInitialized()).toBe(false)
  })

  it('schema 无 plugin_contributes 字段：直接返回，不置初始化标记', () => {
    registry.registerFromSchema({})
    expect(registry.isInitialized()).toBe(false)
    expect(registry.getPages()).toEqual([])
  })

  it('plugin_contributes 为空数组：置初始化标记且无页面', () => {
    registry.registerFromSchema({ plugin_contributes: [] })
    expect(registry.isInitialized()).toBe(true)
    expect(registry.getPages()).toEqual([])
  })

  it('入口项缺 contributes 时跳过该项（其余项照常注册）', () => {
    registry.registerFromSchema({
      plugin_contributes: [
        { plugin_id: 'no-contrib' },
        {
          plugin_id: 'with-contrib',
          contributes: { pages: [{ id: 'p1', title: 'P1', space: 'settings' }] },
        },
      ],
    })
    expect(registry.getPages().map((p) => p.id)).toEqual(['p1'])
  })

  it('贡献 key 的值非数组时跳过（不抛错）', () => {
    registry.registerFromSchema({
      plugin_contributes: [
        {
          plugin_id: 'p',
          contributes: { pages: 'not-an-array' as never },
        },
      ],
    })
    expect(registry.getPages()).toEqual([])
  })

  it.each(['workspaceTabs', 'chatInteractions', 'chatActions'])(
    '弃用贡献 key %s 被忽略（不进 pages 归一化）',
    (key) => {
      registry.registerFromSchema({
        plugin_contributes: [
          { plugin_id: 'p', contributes: { [key]: [{ id: 'x', title: 'X' }] } },
        ],
      })
      expect(registry.getPages()).toEqual([])
    },
  )
})

describe('ContributionRegistry — 归一化 space/slot 映射', () => {
  it.each([
    ['viewsContainers', 'workspace', 'activity-bar'],
    ['views', 'workspace', 'tab'],
    ['dockItems', 'dock', 'item'],
    ['statusBarItems', 'dock', 'status'],
    ['floating', 'floating', 'panel'],
    ['modal', 'floating', 'overlay'],
    ['menus', 'chat', 'inline'],
    ['commands', 'chat', 'input-action'],
    ['shortcuts', 'chat', 'input-action'],
    ['chatMessages', 'chat', 'message-style'],
    ['settingsPanels', 'settings', 'nav'],
    ['widgets', 'workspace', 'tab'],
  ])('legacy key %s → space=%s / slot=%s（legacyFrom 标记来源）', (key, space, slot) => {
    registry.registerFromSchema({
      plugin_contributes: [
        { plugin_id: 'plug', contributes: { [key]: [{ id: `${key}-1`, title: 'T' }] } },
      ],
    })
    const page = registry.getPages()[0]
    expect(page).toMatchObject({ id: `${key}-1`, space, slot, legacyFrom: key })
  })

  it('映射表外的未知 key 兜底归一化为 workspace/tab（不丢弃）', () => {
    registry.registerFromSchema({
      plugin_contributes: [
        { plugin_id: 'plug', contributes: { brandNewThing: [{ id: 'n1', title: 'N' }] } },
      ],
    })
    const page = registry.getPages()[0]
    expect(page).toMatchObject({ id: 'n1', space: 'workspace', slot: 'tab', legacyFrom: 'brandNewThing' })
  })

  it('pages 声明按原样注册：space 取自条目，无 legacyFrom', () => {
    registry.registerFromSchema({
      plugin_contributes: [
        { plugin_id: 'plug', contributes: { pages: [{ id: 'pg', title: 'PG', space: 'debug_center' }] } },
      ],
    })
    const page = registry.getPages()[0]
    expect(page).toMatchObject({ id: 'pg', space: 'debug_center' })
    expect(page.legacyFrom).toBeUndefined()
  })

  it('pages 声明缺 space 时落默认 workspace', () => {
    registry.registerFromSchema({
      plugin_contributes: [{ plugin_id: 'plug', contributes: { pages: [{ id: 'pg' }] } }],
    })
    expect(registry.getPages()[0].space).toBe('workspace')
  })

  it('pages 声明显式 slot 生效（override 为空时不覆盖）', () => {
    registry.registerFromSchema({
      plugin_contributes: [
        { plugin_id: 'plug', contributes: { pages: [{ id: 'pg', slot: 'overlay' }] } },
      ],
    })
    expect(registry.getPages()[0].slot).toBe('overlay')
  })

  it('显式字段被收窄，旧类型扩展字段原样透传', () => {
    registry.registerFromSchema({
      plugin_contributes: [
        {
          plugin_id: 'plug',
          contributes: {
            menus: [{ id: 'm1', command: 'cmd.x', location: 'chat/context', category: 'edit' }],
          },
        },
      ],
    })
    const page = registry.getPages()[0] as Record<string, unknown> & { id: string }
    expect(page.command).toBe('cmd.x')
    expect(page.location).toBe('chat/context')
    expect(page.category).toBe('edit')
  })
})

describe('ContributionRegistry — 无 id 条目的合成 id', () => {
  it('shortcuts 用 command 合成 id', () => {
    registry.registerFromSchema({
      plugin_contributes: [
        { plugin_id: 'plug', contributes: { shortcuts: [{ command: 'do.thing' }] } },
      ],
    })
    expect(registry.getPages()[0].id).toBe('plug:shortcut:do.thing')
  })

  it('menus 用 command + location 合成 id（同命令不同位置不互相去重）', () => {
    registry.registerFromSchema({
      plugin_contributes: [
        {
          plugin_id: 'plug',
          contributes: {
            menus: [
              { command: 'c1', location: 'chat/context' },
              { command: 'c1', location: 'workspace/context' },
            ],
          },
        },
      ],
    })
    expect(registry.getPages().map((p) => p.id)).toEqual([
      'plug:menu:c1:chat/context',
      'plug:menu:c1:workspace/context',
    ])
  })

  it('menus 缺 command/location 时合成空段 id（不抛错）', () => {
    registry.registerFromSchema({
      plugin_contributes: [{ plugin_id: 'plug', contributes: { menus: [{ title: 'M' }] } }],
    })
    expect(registry.getPages()[0].id).toBe('plug:menu::')
  })

  it('其他类型有 command 字段时用 command 合成 id', () => {
    registry.registerFromSchema({
      plugin_contributes: [{ plugin_id: 'plug', contributes: { commands: [{ command: 'run' }] } }],
    })
    expect(registry.getPages()[0].id).toBe('plug:commands:run')
  })

  it('无 command 有 title 时用 title 合成 id', () => {
    registry.registerFromSchema({
      plugin_contributes: [{ plugin_id: 'plug', contributes: { commands: [{ title: '运行' }] } }],
    })
    expect(registry.getPages()[0].id).toBe('plug:commands:运行')
  })

  it('command/title 均缺时用条目 JSON 前缀合成 id（仍有稳定 id）', () => {
    registry.registerFromSchema({
      plugin_contributes: [
        { plugin_id: 'plug', contributes: { commands: [{ category: 'x' }] } },
      ],
    })
    const id = registry.getPages()[0].id
    expect(id.startsWith('plug:commands:')).toBe(true)
    expect(id.length).toBeGreaterThan('plug:commands:'.length)
  })
})

describe('ContributionRegistry — 去重、排序与索引', () => {
  it('同插件重复声明同 id 只保留首个（去重）；跨插件同 id 并存（BUG-11 契约）', () => {
    registry.registerFromSchema({
      plugin_contributes: [
        { plugin_id: 'a', contributes: { pages: [{ id: 'dup', title: '第一次' }, { id: 'dup', title: '重声明' }] } },
        { plugin_id: 'b', contributes: { pages: [{ id: 'dup', title: 'B 的同名页' }] } },
      ],
    })
    // 同插件内重复声明去重，保留首个
    const dupA = registry.getPluginPages('a')
    expect(dupA).toHaveLength(1)
    expect(dupA[0].title).toBe('第一次')
    // 跨插件同名是合法并存（页面 id 命名空间归插件所有）
    expect(registry.getPluginPages('b').map((p) => p.title)).toEqual(['B 的同名页'])
  })

  it('页面按 order 升序排序，缺省 order 视为 50', () => {
    registry.registerFromSchema({
      plugin_contributes: [
        {
          plugin_id: 'p',
          contributes: {
            pages: [
              { id: 'c', title: 'C', order: 90 },
              { id: 'a', title: 'A', order: 10 },
              { id: 'b', title: 'B' },
            ],
          },
        },
      ],
    })
    expect(registry.getPages().map((p) => p.id)).toEqual(['a', 'b', 'c'])
  })

  it('getPluginPages 按插件索引返回且互不串扰', () => {
    registry.registerFromSchema({
      plugin_contributes: [
        { plugin_id: 'a', contributes: { pages: [{ id: 'a1' }, { id: 'a2' }] } },
        { plugin_id: 'b', contributes: { pages: [{ id: 'b1' }] } },
      ],
    })
    expect(registry.getPluginPages('a').map((p) => p.id)).toEqual(['a1', 'a2'])
    expect(registry.getPluginPages('b').map((p) => p.id)).toEqual(['b1'])
    expect(registry.getPluginPages('ghost')).toEqual([])
  })

  it('getPage 未命中返回 undefined', () => {
    registry.registerFromSchema({
      plugin_contributes: [{ plugin_id: 'a', contributes: { pages: [{ id: 'a1' }] } }],
    })
    expect(registry.getPage('a1')).toBeDefined()
    expect(registry.getPage('nope')).toBeUndefined()
  })

  it('公开 register(entry) 走同一归一化链路（含 pluginId 缺省为空串）', () => {
    registry.register({ type: 'pages', id: 'direct-1', title: 'D1', space: 'settings' } as never)
    registry.register({ type: 'views', id: 'direct-2', title: 'D2' } as never)
    const pages = registry.getPages()
    expect(pages.find((p) => p.id === 'direct-1')).toMatchObject({ space: 'settings' })
    expect(pages.find((p) => p.id === 'direct-2')).toMatchObject({
      space: 'workspace',
      slot: 'tab',
      legacyFrom: 'views',
      pluginId: '',
    })
  })

  it('getPagesBySpace 只返回该空间页面', () => {
    registry.registerFromSchema({
      plugin_contributes: [
        {
          plugin_id: 'p',
          contributes: {
            pages: [
              { id: 's1', space: 'settings' },
              { id: 'w1', space: 'workspace' },
              { id: 's2', space: 'settings' },
            ],
          },
        },
      ],
    })
    expect(registry.getPagesBySpace('settings').map((p) => p.id)).toEqual(['s1', 's2'])
    expect(registry.getPagesBySpace('dock')).toEqual([])
  })
})

describe('ContributionRegistry — 注销与薄视图', () => {
  function seed() {
    registry.registerFromSchema({
      plugin_contributes: [
        {
          plugin_id: 'plug',
          contributes: {
            pages: [{ id: 'pg1', title: '声明页' }],
            views: [{ id: 'v1', containerId: 'sidebar' }],
            menus: [{ id: 'm1', location: 'chat/context' }],
            commands: [{ id: 'c1' }],
            shortcuts: [{ id: 's1' }],
            modal: [{ id: 'mo1', trigger: 'on_command:x' }],
          },
        },
      ],
    })
  }

  it('unregister 移除匹配条目并重建插件索引', () => {
    seed()
    registry.unregister('views', 'v1')
    expect(registry.getPage('v1')).toBeUndefined()
    expect(registry.getPluginPages('plug').map((p) => p.id)).not.toContain('v1')
    expect(registry.getPluginPages('plug').map((p) => p.id)).toContain('pg1')
  })

  it('unregister 未命中时不改动任何数据（索引保持有效）', () => {
    seed()
    const before = registry.getPages().map((p) => p.id)
    registry.unregister('views', 'ghost')
    expect(registry.getPages().map((p) => p.id)).toEqual(before)
  })

  it('unregister pages 类型只匹配非 legacyFrom 项（声明页与归一化页区分）', () => {
    seed()
    // views 归一化页 id=v1 有 legacyFrom，按 'pages' 类型注销不应命中
    registry.unregister('pages', 'v1')
    expect(registry.getPage('v1')).toBeDefined()
    registry.unregister('pages', 'pg1')
    expect(registry.getPage('pg1')).toBeUndefined()
  })

  it('getByType(pages) 只返回声明页；旧 key 只返回 legacyFrom 匹配项', () => {
    seed()
    expect(registry.getByType('pages').map((p) => p.id)).toEqual(['pg1'])
    expect(registry.getByType('views').map((p) => p.id)).toEqual(['v1'])
    expect(registry.getByType('dockItems')).toEqual([])
  })

  it('getViews 省略 containerId 返回全部视图，指定则过滤', () => {
    registry.registerFromSchema({
      plugin_contributes: [
        {
          plugin_id: 'p',
          contributes: {
            views: [
              { id: 'v1', containerId: 'sidebar' },
              { id: 'v2', containerId: 'panel' },
            ],
          },
        },
      ],
    })
    expect(registry.getViews().map((v) => v.id)).toEqual(['v1', 'v2'])
    expect(registry.getViews('sidebar').map((v) => v.id)).toEqual(['v1'])
    expect(registry.getViews('nope')).toEqual([])
  })

  it('getMenus 省略 location 返回全部；指定则精确过滤', () => {
    registry.registerFromSchema({
      plugin_contributes: [
        {
          plugin_id: 'p',
          contributes: {
            menus: [
              { id: 'm1', location: 'chat/context' },
              { id: 'm2', location: 'workspace/context' },
            ],
          },
        },
      ],
    })
    expect(registry.getMenus().map((m) => m.id)).toEqual(['m1', 'm2'])
    expect(registry.getMenus('chat/context').map((m) => m.id)).toEqual(['m1'])
  })

  it('getCommands/getShortcuts/getModals 按 legacyFrom 分别检索', () => {
    seed()
    expect(registry.getCommands().map((p) => p.id)).toEqual(['c1'])
    expect(registry.getShortcuts().map((p) => p.id)).toEqual(['s1'])
    expect(registry.getModals().map((p) => p.id)).toEqual(['mo1'])
  })

  it('findModalByTrigger 命中 trigger 与 openOn 两种写法，未命中 undefined', () => {
    registry.registerFromSchema({
      plugin_contributes: [
        {
          plugin_id: 'p',
          contributes: {
            modal: [
              { id: 'mo-trigger', trigger: 'on_command:a' },
              { id: 'mo-openon', openOn: 'on_command:b' },
            ],
          },
        },
      ],
    })
    expect(registry.findModalByTrigger('on_command:a')?.id).toBe('mo-trigger')
    expect(registry.findModalByTrigger('on_command:b')?.id).toBe('mo-openon')
    expect(registry.findModalByTrigger('on_command:c')).toBeUndefined()
  })
})

describe('ContributionRegistry — clear 全量复位', () => {
  it('clear 清空页面/配置面板/widget/主题/样式并复位初始化标记', () => {
    registry.loadFromSchema({
      plugin_configs: [
        { plugin_id: 'cfg', plugin_name: 'Cfg', config_files: [{ id: 'f1', path: '/a.yaml', label: 'A' }] },
      ],
      agents: [{ id: 'agent-1', ui_schema: { widgets: [{ id: 'w1', type: 'form' }] } }],
      plugin_contributes: [
        {
          plugin_id: 'plug',
          contributes: {
            pages: [{ id: 'pg' }],
            themes: [{ id: 'th', name: 'T' }],
            client_styles: [{ id: 'st', path: '/s.css' }],
          },
        },
      ],
    })
    expect(registry.isInitialized()).toBe(true)
    expect(registry.getPages().length).toBeGreaterThan(0)

    registry.clear()

    expect(registry.isInitialized()).toBe(false)
    expect(registry.getPages()).toEqual([])
    expect(registry.getPluginPages('plug')).toEqual([])
    expect(registry.getSettingsPanels()).toEqual([])
    expect(registry.getAllWidgets()).toEqual([])
    expect(registry.getPluginThemes()).toEqual([])
    expect(registry.getClientStyles()).toEqual([])
    expect(registry.hasPluginConfig('cfg')).toBe(false)
  })

  it('loadFromSchema 内部先 clear（幂等重载不留幽灵页）', () => {
    registry.registerFromSchema({
      plugin_contributes: [{ plugin_id: 'old', contributes: { pages: [{ id: 'old-page' }] } }],
    })
    registry.loadFromSchema({ plugin_contributes: [] })
    expect(registry.getPage('old-page')).toBeUndefined()
    expect(registry.isInitialized()).toBe(true)
  })
})

describe('ContributionRegistry — 配置面板与 widget 声明', () => {
  it('settingsPanels 注册后可按 pluginId 查询单条', () => {
    registry.loadFromSchema({
      plugin_configs: [
        { plugin_id: 'p1', plugin_name: 'P1', config_files: [{ id: 'f', path: '/p.yaml', label: 'L' }] },
      ],
    })
    expect(registry.getSettingsPanel('p1')?.pluginName).toBe('P1')
    expect(registry.getSettingsPanel('ghost')).toBeUndefined()
  })

  it('plugin_configs 非数组时跳过（不抛错）', () => {
    registry.loadFromSchema({ plugin_configs: 'oops' as never })
    expect(registry.getSettingsPanels()).toEqual([])
  })

  it('config_files 空数组时不产生 settings 页面', () => {
    registry.loadFromSchema({
      plugin_configs: [{ plugin_id: 'p1', plugin_name: 'P1', config_files: [] }],
    })
    expect(registry.getPages()).toEqual([])
    expect(registry.hasPluginConfig('p1')).toBe(true)
  })

  it('widget 声明来源三选（agents/pipelines/plugin_contributes），无 id 或缺 ui_schema 的跳过', () => {
    registry.loadFromSchema({
      agents: [
        { id: 'a1', ui_schema: { widgets: [{ id: 'wa', type: 'form' }] } },
        { ui_schema: { widgets: [{ id: 'no-id-source', type: 'form' }] } },
        { id: 'a3' },
      ],
      pipelines: [
        { id: 'pl1', ui_schema: { widgets: [{ id: 'wp', type: 'chart', order: 3 }] } },
        { id: 'pl2', ui_schema: { widgets: 'bad' as never } },
      ],
      plugin_contributes: [
        { plugin_id: 'pc1', contributes: {}, ui_schema: { widgets: [{ id: 'wc', type: 'table' }] } },
      ],
    })
    expect(registry.getWidgetsForPlugin('a1').map((w) => w.id)).toEqual(['wa'])
    expect(registry.getWidgetsForPlugin('a3')).toEqual([])
    expect(registry.getWidgetsForPlugin('pl1')).toMatchObject([{ id: 'wp', order: 3 }])
    expect(registry.getWidgetsForPlugin('pc1').map((w) => w.id)).toEqual(['wc'])
    expect(registry.getAllWidgets().map((w) => w.id).sort()).toEqual(['wa', 'wc', 'wp'])
    expect(registry.getWidgetsForPlugin('ghost')).toEqual([])
  })

  it('widget order 非数字时归一化为 undefined', () => {
    registry.loadFromSchema({
      agents: [{ id: 'a1', ui_schema: { widgets: [{ id: 'w', type: 'form', order: 'x' }] } }],
    })
    expect(registry.getWidgetsForPlugin('a1')[0].order).toBeUndefined()
  })
})
