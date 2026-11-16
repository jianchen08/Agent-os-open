/**
 * 首帧主题外观专用键（theme-appearance）写入单点（P-74/P-75 修复）。
 *
 * 背景：首帧防闪脚本 public/theme-init.js（index.html 同步外链，先于渲染执行）
 * 与 themeStore 分属两个执行世界——前者是 public/ 静态文件（无打包器处理，不可
 * import src 模块），后者经 zustand persist 落盘；persist 的 theme-storage 只存
 * mode+currentThemeId，插件深色皮肤/用户动态深色主题的明暗无法在首帧静态判定
 * （旧实现靠 theme-init 侧硬编码 id 白名单，新深色主题必漏）。
 *
 * 契约：themeStore.applyTheme 每次成功应用主题时调用 writeThemeAppearance 同步
 * 写本键（值恒为 'dark'|'light' 纯字符串，非 JSON 包装），theme-init.js 优先读
 * 它作首帧判定——单一事实源，天然覆盖插件/用户主题；theme-storage 的 id 白名单
 * 仅作迁移窗口兜底。
 *
 * ⚠ 键名 'theme-appearance'、值格式与前缀解析（userScopedKey）在
 *   frontend/public/theme-init.js 有同步约束下的小面复制，两处注释锚点互指，
 *   改任一侧必须同步另一侧（防再漂移）。
 */
import { userScopedKey } from './userScopedStorage'

/** 首帧外观专用键业务键名（落盘带分户前缀，见 userScopedKey） */
export const THEME_APPEARANCE_KEY = 'theme-appearance'

/** 首帧明暗外观（与 themeStore resolvedTheme 同一 isLightTheme 派生） */
export type ThemeAppearance = 'dark' | 'light'

/**
 * 写首帧外观专用键（themeStore.applyTheme 单点调用）。
 *
 * 写失败（配额耗尽/存储被禁）按未写处理：theme-init 回退 theme-storage id
 * 白名单判定，最坏退化为迁移窗口内一次闪色——本键是首帧提示缓存而非权威
 * 状态（权威选择在 theme-storage），失败降级是定义行为，不向调用方传播。
 */
export function writeThemeAppearance(appearance: ThemeAppearance): void {
  try {
    localStorage.setItem(userScopedKey(THEME_APPEARANCE_KEY), appearance)
  } catch {
    // 定义降级：见上方契约注释
  }
}
