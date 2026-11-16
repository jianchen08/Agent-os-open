/* global localStorage, document */
/**
 * 首帧主题预置脚本（index.html 同步外链加载，先于渲染执行防主题闪色）。
 *
 * 外置原因：生产 CSP（nginx.conf）script-src 不允许内联脚本，页面不得残留
 * 内联 <script>；本文件为同源静态文件，head 内同步执行。主题切换后的持久化
 * 与 class 写回由 themeStore（zustand persist）承担，本脚本只负责刷新/首访时
 * 提前判定明暗并给 <html> 加 dark class。
 *
 * 判定链（键名、值格式与分户前缀解析是 src/utils/themeAppearance.ts +
 * src/utils/userScopedStorage.ts 在同步约束下的小面复制——本脚本先于打包
 * 产物执行不可 import，两处注释锚点互指，改任一侧必须同步另一侧）：
 * 1. 专用键 <分户前缀>theme-appearance（'dark'|'light' 纯字符串，themeStore
 *    applyTheme 每次成功应用主题同步写）——权威档，天然覆盖插件深色皮肤与
 *    用户动态深色主题；
 * 2. 分户 theme-storage（zustand persist 实际落盘键）currentThemeId × 预设
 *    深色集——迁移窗口兜底（升级后首次刷新专用键尚不存在）；
 * 3. 历史裸键 theme-storage（分户改造前数据，cacheBoundary 清扫前兼容）。
 * 皆未命中 → 默认浅色（与 themeStore 默认 light 同源，不加 class）。
 */
;(function () {
  // 分户前缀解析：与 userScopedStorage.userScopedKey 小面复制（锚点互指）。
  // 身份源=localStorage AUTH_USER 的 id 字段（此时注册表不存在，只有磁盘
  // 回退路径），畸形档按匿名——与 userScopedStorage.identityFromStorage 同向。
  function scopedKey(base) {
    var prefix = 'ag-anon:'
    try {
      var raw = localStorage.getItem('auth_user')
      if (raw) {
        var id = JSON.parse(raw).id
        if (typeof id === 'string' && id) prefix = 'ag-u:' + id + ':'
      }
    } catch {
      // 畸形 AUTH_USER → 匿名命名空间
    }
    return prefix + base
  }

  function currentThemeIdFrom(raw) {
    try {
      var saved = JSON.parse(raw || '{}')
      return (saved.state && saved.state.currentThemeId) || null
    } catch {
      return null
    }
  }

  try {
    // 权威档：专用键命中（'dark'/'light'）即终裁，不再看 theme-storage。
    var appearance = localStorage.getItem(scopedKey('theme-appearance'))
    var isDark
    if (appearance === 'dark') {
      isDark = true
    } else if (appearance === 'light') {
      isDark = false
    } else {
      // 迁移窗口兜底：专用键缺失/异值 → persist 分户键 → 历史裸键。
      // 预设深色 id 集 = src/config/themes/presets 中经 themeStore.isLightTheme
      // 派生为 dark 的集合（id 不含 'light' 且 category 非 light）。插件/用户
      // 深色主题不走这里（专用键已覆盖）；新增深色预设需同步本清单（仅迁移
      // 窗口一次性成本）。
      var presetDarkIds = ['dark', 'deep-space', 'high-contrast']
      var id = currentThemeIdFrom(localStorage.getItem(scopedKey('theme-storage')))
      if (!id) id = currentThemeIdFrom(localStorage.getItem('theme-storage'))
      isDark = !!id && presetDarkIds.indexOf(id) !== -1
    }
    if (isDark) document.documentElement.classList.add('dark')
  } catch {
    // 无缓存/解析失败 → 默认浅色（与 themeStore 默认值同源，不加 dark class）
  }
})()
