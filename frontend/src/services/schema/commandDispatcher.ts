/**
 * CommandDispatcher（P5 a/b/c/d 核心，ADR §3.4 档位二）
 *
 * 命令是 P5 四类插槽的统一出口：
 *   菜单点击 / 命令面板执行 / 快捷键命中 / modal trigger
 *   → 全部经 CommandDispatcher.executeCommand(commandId, args)
 *   → 路由到内核（插件 capability，经 transport）或打开声明的 modal
 *
 * 设计要点：
 * - transport 注入（仅这一段是外部依赖，可 Mock）；被测逻辑不 Mock
 * - modal 触发：若存在 trigger=on_command:<commandId> 的 modal 声明，执行命令后打开 modal
 * - when 过滤：命令/菜单的 when 字段经 evaluateWhen(contextKeys) 过滤可见性
 */

import { contributionRegistry, type ContributionEntry } from '@/services/schema/ContributionRegistry'
import { evaluateWhen, type ContextKeys } from '@/services/schema/whenExpression'
import { useContextKeys } from '@/stores/contextKeysStore'

/** 内核 transport：把命令交给内核执行（最终调插件 capability/tool） */
export type CommandTransport = (commandId: string, args?: unknown) => Promise<unknown>

/** modal 打开回调 */
export type ModalOpenHandler = (modal: ContributionEntry) => void

/** commandId → trigger 字符串约定 */
const COMMAND_TRIGGER_PREFIX = 'on_command:'

export class CommandDispatcher {
  /** 注入的内核 transport（命令真正执行的出口） */
  private transport: CommandTransport | null = null
  /** modal 打开订阅者 */
  private modalOpenHandlers: ModalOpenHandler[] = []
  /** 数据源（默认全局单例，可注入便于测试） */
  private readonly registry: { getCommands: () => ContributionEntry[]; getMenus: (location?: string) => ContributionEntry[]; getModals: () => ContributionEntry[]; findModalByTrigger: (trigger: string) => ContributionEntry | undefined }
  /** context keys 读取器（默认读全局 store，可注入便于测试） */
  private readonly getKeys: () => ContextKeys

  constructor(
    registry?: { getCommands: () => ContributionEntry[]; getMenus: (location?: string) => ContributionEntry[]; getModals: () => ContributionEntry[]; findModalByTrigger: (trigger: string) => ContributionEntry | undefined },
    getKeys?: () => ContextKeys,
  ) {
    this.registry = registry ?? contributionRegistry
    this.getKeys = getKeys ?? (() => useContextKeys.getState().keys)
  }

  /** 注入内核 transport（启动期由 GrowthLoop 注入） */
  setTransport(transport: CommandTransport): void {
    this.transport = transport
  }

  /** 订阅 modal 打开事件（ModalHost 注册） */
  onModalOpen(handler: ModalOpenHandler): () => void {
    this.modalOpenHandlers.push(handler)
    return () => {
      const idx = this.modalOpenHandlers.indexOf(handler)
      if (idx >= 0) this.modalOpenHandlers.splice(idx, 1)
    }
  }

  /**
   * 执行命令：路由到内核 transport + 触发绑定的 modal
   *
   * @param commandId - 命令标识
   * @param args - 命令参数（透传给内核）
   */
  async executeCommand(commandId: string, args?: unknown): Promise<void> {
    // 1. 路由到内核（插件 capability）
    if (this.transport) {
      try {
        await this.transport(commandId, args)
      } catch {
        // transport 失败不阻塞 modal 触发；错误处理由 transport 自身负责
      }
    }

    // 2. 触发绑定的 modal（trigger=on_command:<commandId>）
    const modal = this.registry.findModalByTrigger(`${COMMAND_TRIGGER_PREFIX}${commandId}`)
    if (modal) {
      for (const handler of this.modalOpenHandlers) {
        handler(modal)
      }
    }
  }

  /**
   * 命令面板搜索（P5-b）：按 title/category 模糊匹配
   *
   * @param query - 搜索词（空返回全部）
   * @returns 命令列表（含可见性过滤）
   */
  searchCommands(query: string): ContributionEntry[] {
    const all = this.registry.getCommands()
    if (!query.trim()) return all
    const q = query.toLowerCase()
    return all.filter((c) => {
      const title = (c.title ?? '').toLowerCase()
      const category = (c.category as string | undefined ?? '').toLowerCase()
      return title.includes(q) || category.includes(q)
    })
  }

  /**
   * 获取当前可见的命令（when 命中）
   */
  getVisibleCommands(): ContributionEntry[] {
    const ctx = this.getKeys()
    return this.registry.getCommands().filter((c) => evaluateWhen(c.when, ctx))
  }

  /**
   * 获取指定位置当前可见的菜单项（when 命中）
   *
   * @param location - 菜单位置
   */
  getVisibleMenus(location: string): ContributionEntry[] {
    const ctx = this.getKeys()
    return this.registry.getMenus(location).filter((m) => evaluateWhen(m.when, ctx))
  }
}

/** 全局单例 */
export const commandDispatcher = new CommandDispatcher()
