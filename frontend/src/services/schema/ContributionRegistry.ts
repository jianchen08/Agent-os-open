/**
 * ContributionRegistry — 统一贡献注册中心
 *
 * ADR §六：前端建立 ContributionRegistry 作为唯一真相源，
 * 所有插件贡献（导航、布局、配置、UI 插槽）从这里派生。
 *
 * 四个投影：
 * - NavigationProvider → 顶栏/侧栏导航项
 * - LayoutProvider → 布局区的存在与拓扑
 * - SettingsProvider → 设置菜单分组 + 配置页
 * - WidgetProvider → 预置 Widget 注册
 *
 * 数据源：/api/v1/schema 聚合的插件 manifest contributes.*
 */

import React from 'react'

/** 贡献点类型 */
export type ContributionType =
  | 'viewsContainers'   // ActivityBar 一级导航
  | 'views'             // 侧边栏视图
  | 'workspaceTabs'     // 工作区标签页
  | 'dockItems'         // 底部 dock 栏
  | 'floating'          // 浮窗
  | 'modal'             // 模态对话框
  | 'statusBarItems'    // 状态栏条目
  | 'menus'             // 右键/顶栏菜单
  | 'commands'          // 命令面板
  | 'shortcuts'         // 快捷键
  | 'chatMessages'      // 聊天消息卡片样式
  | 'chatInteractions'  // 聊天交互模式
  | 'chatActions'       // 聊天输入区动作
  | 'settingsPanels'    // 插件配置面板
  | 'widgets'           // 预置 widget 注册

/** 单个贡献条目 */
export interface ContributionEntry {
  /** 贡献点类型 */
  type: ContributionType
  /** 唯一标识 */
  id: string
  /** 显示名称 */
  title?: string
  /** 图标 */
  icon?: string
  /** 何时可见（条件表达式） */
  when?: string
  /** 何时激活（触发器） */
  openOn?: string
  /** 使用哪个预置 widget */
  widget?: string
  /** widget 配置/数据源 */
  props?: Record<string, unknown>
  /** 排序权重 */
  order?: number
  /** 所属插件 ID */
  pluginId?: string
  /** 配置文件路径（settingsPanels 用） */
  configPath?: string
  /** 配置标签（settingsPanels 用） */
  configLabel?: string
  /** 路由路径（viewsContainers 用） */
  path?: string
  /** 扩展字段 */
  [key: string]: unknown
}

/** 配置面板条目（settingsPanels 专用） */
export interface SettingsPanelEntry {
  pluginId: string
  pluginName: string
  pluginIcon?: string
  configFiles: Array<{
    id: string
    path: string
    label: string
  }>
}

/**
 * ContributionRegistry
 *
 * 全局单例，管理所有插件贡献。
 * 数据源来自 /api/v1/schema 聚合。
 */
class ContributionRegistry {
  /** 所有贡献条目 */
  private entries: Map<string, ContributionEntry[]> = new Map()
  /** 配置面板条目 */
  private settingsPanels: Map<string, SettingsPanelEntry> = new Map()
  /** 是否已初始化 */
  private initialized = false

  /**
   * 从 /api/v1/schema 响应加载贡献（含 plugin_configs）
   *
   * @param schema - SchemaResponse 对象
   */
  loadFromSchema(schema: Record<string, unknown>): void {
    // 加载 plugin_configs（配置面板）
    const pluginConfigs = (schema as Record<string, unknown>).plugin_configs as
      | Array<{ plugin_id: string; plugin_name: string; config_files: Array<{ id: string; path: string; label: string }> }>
      | undefined

    if (Array.isArray(pluginConfigs)) {
      for (const entry of pluginConfigs) {
        this.settingsPanels.set(entry.plugin_id, {
          pluginId: entry.plugin_id,
          pluginName: entry.plugin_name,
          configFiles: entry.config_files,
        })
      }
    }

    // 加载 modules（contributes / ui_contributions）
    this.registerFromSchema(schema as Parameters<typeof this.registerFromSchema>[0])
  }

  /**
   * 获取指定插件的配置文件列表
   */
  getPluginConfigFiles(pluginId: string): Array<{ id: string; path: string; label: string }> {
    return this.settingsPanels.get(pluginId)?.configFiles ?? []
  }

  /**
   * 获取全部插件配置条目
   */
  getPluginConfigs(): SettingsPanelEntry[] {
    return Array.from(this.settingsPanels.values())
  }

  /**
   * 从 schema 数据注册贡献
   *
   * @param schemaData - /api/v1/schema 返回的聚合数据
   */
  registerFromSchema(schemaData: {
    modules?: Array<{
      module_id: string
      name?: string
      icon?: string
      ui_contributions?: Array<Record<string, unknown>>
      config_files?: Array<{ id: string; path: string; label: string }>
      contributes?: Record<string, unknown[]>
    }>
  }): void {
    if (!schemaData.modules) return

    for (const module of schemaData.modules) {
      const pluginId = module.module_id

      // 注册 ui_contributions
      if (module.ui_contributions) {
        for (const contrib of module.ui_contributions) {
          this.register({
            ...contrib,
            pluginId,
          } as ContributionEntry)
        }
      }

      // 注册 contributes（manifest 内联）
      if (module.contributes) {
        for (const [type, items] of Object.entries(module.contributes)) {
          for (const item of items as Array<Record<string, unknown>>) {
            this.register({
              ...item,
              type: type as ContributionType,
              pluginId,
            } as ContributionEntry)
          }
        }
      }

      // 注册配置面板（config_files → settingsPanels）
      if (module.config_files && module.config_files.length > 0) {
        this.settingsPanels.set(pluginId, {
          pluginId,
          pluginName: module.name || pluginId,
          pluginIcon: module.icon,
          configFiles: module.config_files,
        })
      }
    }

    this.initialized = true
  }

  /**
   * 注册单个贡献条目
   */
  register(entry: ContributionEntry): void {
    const type = entry.type
    if (!this.entries.has(type)) {
      this.entries.set(type, [])
    }
    const list = this.entries.get(type)!
    // 去重
    if (!list.some((e) => e.id === entry.id)) {
      list.push(entry)
      // 按 order 排序
      list.sort((a, b) => (a.order ?? 50) - (b.order ?? 50))
    }
  }

  /**
   * 注销贡献条目
   */
  unregister(type: ContributionType, id: string): void {
    const list = this.entries.get(type)
    if (list) {
      const idx = list.findIndex((e) => e.id === id)
      if (idx >= 0) list.splice(idx, 1)
    }
  }

  /**
   * 获取某类型的所有贡献
   */
  getByType(type: ContributionType): ContributionEntry[] {
    return this.entries.get(type) ?? []
  }

  /**
   * 获取导航项（viewsContainers）
   */
  getViewsContainers(): ContributionEntry[] {
    return this.getByType('viewsContainers')
  }

  /**
   * 获取侧边栏视图（views）
   */
  getViews(containerId?: string): ContributionEntry[] {
    const views = this.getByType('views')
    if (containerId) {
      return views.filter((v) => v.containerId === containerId)
    }
    return views
  }

  /**
   * 获取工作区标签页（workspaceTabs）
   */
  getWorkspaceTabs(): ContributionEntry[] {
    return this.getByType('workspaceTabs')
  }

  /**
   * 获取配置面板列表（settingsPanels）
   */
  getSettingsPanels(): SettingsPanelEntry[] {
    return Array.from(this.settingsPanels.values())
  }

  /**
   * 获取指定插件的配置面板
   */
  getSettingsPanel(pluginId: string): SettingsPanelEntry | undefined {
    return this.settingsPanels.get(pluginId)
  }

  /**
   * 获取状态栏条目
   */
  getStatusBarItems(): ContributionEntry[] {
    return this.getByType('statusBarItems')
  }

  /**
   * 获取 dock 栏条目
   */
  getDockItems(): ContributionEntry[] {
    return this.getByType('dockItems')
  }

  /**
   * 检查是否已初始化
   */
  isInitialized(): boolean {
    return this.initialized
  }

  /**
   * 清空所有注册
   */
  clear(): void {
    this.entries.clear()
    this.settingsPanels.clear()
    this.initialized = false
  }
}

/** 全局单例 */
export const contributionRegistry = new ContributionRegistry()
