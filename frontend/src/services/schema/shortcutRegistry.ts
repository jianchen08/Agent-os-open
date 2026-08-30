/**
 * ShortcutRegistry（P5-c 快捷键，ADR §3.4 档位二）
 *
 * 从 contributes.shortcuts 加载 { command, key, when }，注册全局键盘监听，
 * 按 when 命中触发对应 command（经 CommandDispatcher）。
 *
 * key 归一化：Ctrl/Cmd/Shift/Alt 修饰键顺序无关、字母小写、Cmd→meta。
 */

import { contributionRegistry, type ContributionEntry } from '@/services/schema/ContributionRegistry'
import { evaluateWhen, type ContextKeys } from '@/services/schema/whenExpression'
import { useContextKeys } from '@/stores/contextKeysStore'

/** 快捷键绑定 */
export interface ShortcutBinding {
  /** 触发的命令 ID */
  command: string
  /** 原始 key 字符串（如 'Ctrl+S'） */
  key: string
  /** 归一化后的 key（如 'ctrl+s'） */
  normalizedKey: string
  /** when 条件 */
  when?: string
  /** 来源插件 */
  pluginId?: string
}

/** 修饰键别名 → 归一化名 */
const MODIFIER_ALIASES: Record<string, string> = {
  ctrl: 'ctrl',
  control: 'ctrl',
  cmd: 'meta',
  command: 'meta',
  meta: 'meta',
  shift: 'shift',
  alt: 'alt',
  option: 'alt',
}

/**
 * 归一化快捷键字符串：拆分 + 、转小写、修饰键排序、普通键保留
 *
 * @param key - 原始 key（如 'Ctrl+Shift+P'）
 * @returns 归一化 key（如 'ctrl+shift+p'）
 */
export function normalizeKey(key: string): string {
  const parts = key
    .split('+')
    .map((p) => p.trim().toLowerCase())
    .filter((p) => p.length > 0)

  const modifiers: string[] = []
  const others: string[] = []
  for (const p of parts) {
    const norm = MODIFIER_ALIASES[p] ?? p
    if (['ctrl', 'meta', 'shift', 'alt'].includes(norm)) {
      modifiers.push(norm)
    } else {
      others.push(norm)
    }
  }
  // 修饰键固定顺序，普通键在后
  const order = { ctrl: 0, shift: 1, alt: 2, meta: 3 }
  modifiers.sort((a, b) => order[a as keyof typeof order] - order[b as keyof typeof order])
  return [...modifiers, ...others].join('+')
}

/** 从 KeyboardEvent 提取归一化 key */
function eventToKey(ev: KeyboardEvent): string {
  const modifiers: string[] = []
  if (ev.ctrlKey) modifiers.push('ctrl')
  if (ev.shiftKey) modifiers.push('shift')
  if (ev.altKey) modifiers.push('alt')
  if (ev.metaKey) modifiers.push('meta')
  const main = (ev.key ?? '').toLowerCase()
  return [...modifiers, main].join('+')
}

export class ShortcutRegistry {
  private bindings: ShortcutBinding[] = []
  private readonly registry: { getShortcuts: () => ContributionEntry[] }
  private readonly getKeys: () => ContextKeys

  constructor(
    registry?: { getShortcuts: () => ContributionEntry[] },
    getKeys?: () => ContextKeys,
  ) {
    this.registry = registry ?? contributionRegistry
    this.getKeys = getKeys ?? (() => useContextKeys.getState().keys)
  }

  /** 从 ContributionRegistry 重新加载快捷键绑定（幂等：先清空） */
  refresh(): void {
    const entries = this.registry.getShortcuts()
    this.bindings = entries.map((e) => ({
      command: e.command as string,
      key: e.key as string,
      normalizedKey: normalizeKey(e.key as string),
      when: e.when,
      pluginId: e.pluginId,
    }))
  }

  /** 获取当前绑定（只读视图） */
  getBindings(): ShortcutBinding[] {
    return [...this.bindings]
  }

  /**
   * 按 KeyboardEvent 查找命中的 command（不检查 when）
   *
   * @returns 命中的 command ID，无命中返回 undefined
   */
  matchKey(ev: KeyboardEvent): string | undefined {
    const key = eventToKey(ev)
    const hit = this.bindings.find((b) => b.normalizedKey === key)
    return hit?.command
  }

  /**
   * 判断某 command 当前是否应触发（when 命中）
   */
  shouldFire(command: string): boolean {
    const binding = this.bindings.find((b) => b.command === command)
    if (!binding) return false
    return evaluateWhen(binding.when, this.getKeys())
  }
}

/** 全局单例 */
export const shortcutRegistry = new ShortcutRegistry()
