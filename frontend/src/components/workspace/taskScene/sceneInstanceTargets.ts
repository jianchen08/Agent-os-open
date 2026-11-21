/**
 * 实例卡点击目标解析（现场面板「运行中实例」→ 点开能到哪）
 *
 * 用户裁定 2026-11-20：「实例应该能够点击打开——比如正在运行的命令能打开对应的
 * 运行窗口，浏览器能打开对应浏览器页面在侧边栏，如果正在用 mcp 操作的应用应该能
 * 弹出对应应用」。
 *
 * 三类目标（按通道，不按目标拆分——实例身份即通道，见 sceneInstances 的契约）：
 *
 * | 通道 | 目标 | 依据 |
 * |---|---|---|
 * | 终端（terminal） | `inline`：面板内「运行窗口」，展示该实例最新调用的命令 + stdout | 复用 `sceneDetails.buildSceneDetailBlocks` 的终端块，不新造终端渲染 |
 * | 浏览器（browser） | `plugin_page`：`browser_tool/browser_live` 工作区侧边页签 | 既有 `autoOpenBrowserLiveOnToolStart` 同一个声明页（ADR 2026-10-21-browser-live-workspace-tab） |
 * | 桌面 / 软件 / 手机（desktop·software·phone） | `inline`：面板内运行窗口（截图 + 活动详情） | 这三类没有已声明的运行时页面，如实走面板内视图，不编造弹窗 |
 *
 * **不可用时的不做死链接**：`hasPage` 查询插件页面声明（`contributionRegistry`），
 * 页面未装载/插件禁用 → 返回 `unavailable` 并带原因文案；卡片按此**不渲染点击态**
 * 并显示原因（用户要求「要么不显示点击态，要么明确提示原因」）。
 *
 * 已知边界（如实）：浏览器页面本身依赖 MCP/浏览器通道，通道不可用时页签能打开但
 * 页面内没有实时画面——这属通道层问题，本模块只保证「点了会到哪里」这一层可用。
 */

import type { SceneToolCategory } from '@/utils/toolCategory'

/** 浏览器直播页声明坐标（与 browserLiveAutoOpen 同一份声明，不重复硬编码新值） */
export const BROWSER_LIVE_PAGE = {
  pluginId: 'browser_tool',
  pageId: 'browser_live',
} as const

/** 实例点击目标（plugin_page 用字面量坐标——openDeclaredPluginPage 只收
 *  DeclaredPluginPageRef 字面量联合，string 宽化在调用点必炸） */
export type SceneInstanceTarget =
  /** 打开声明式插件页面（工作区侧边栏页签） */
  | {
      kind: 'plugin_page'
      pluginId: typeof BROWSER_LIVE_PAGE.pluginId
      pageId: typeof BROWSER_LIVE_PAGE.pageId
      label: string
    }
  /** 面板内运行窗口（终端输出 / 设备活动详情） */
  | { kind: 'inline'; label: string }
  /** 无可用目标：不渲染点击态，显示原因 */
  | { kind: 'unavailable'; reason: string }

/** 页面声明存在性查询（生产实现注入 contributionRegistry，测试可注入假查询） */
export type HasPluginPage = (pluginId: string, pageId: string) => boolean

/**
 * 按通道类别解析点击目标。
 *
 * @param category 实例通道类别
 * @param hasPage 页面声明存在性查询（提供时可在解析期判定 plugin_page 可用性）
 */
export function resolveInstanceTarget(
  category: SceneToolCategory,
  hasPage?: HasPluginPage,
): SceneInstanceTarget {
  switch (category) {
    case 'terminal':
      return { kind: 'inline', label: '运行窗口' }

    case 'browser': {
      const { pluginId, pageId } = BROWSER_LIVE_PAGE
      if (hasPage && !hasPage(pluginId, pageId)) {
        return {
          kind: 'unavailable',
          reason: '浏览器直播页未装载（browser_tool 插件未启用或页面声明已更名）',
        }
      }
      return { kind: 'plugin_page', pluginId, pageId, label: '浏览器页面' }
    }

    case 'desktop':
    case 'software':
    case 'phone':
      // 无已声明的运行时页面：走面板内视图（截图 + 活动详情），不假装能弹出外部应用
      return { kind: 'inline', label: '运行窗口' }

    default:
      // file/other 不成实例（见 sceneInstances），兜底给不可用而非假链接
      return { kind: 'unavailable', reason: '该通道没有可打开的运行视图' }
  }
}

/** 目标是否可点击（unavailable 一律不可点） */
export function isActionableTarget(target: SceneInstanceTarget): boolean {
  return target.kind !== 'unavailable'
}
