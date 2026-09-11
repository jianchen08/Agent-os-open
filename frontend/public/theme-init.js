/* global localStorage, document */
/**
 * 首帧主题预置脚本（index.html 同步外链加载，先于渲染执行防主题闪色）。
 *
 * 外置原因：生产 CSP（nginx.conf）script-src 不允许内联脚本，本脚本是
 * index.html 唯一内联脚本，外置为同源静态文件后无需为它开内联豁免。
 * 主题切换后的持久化与 class 写回由 themeStore（zustand persist）承担，
 * 本脚本只负责刷新/首访时按 theme-storage 提前加 dark/light class。
 */
;(function () {
  try {
    var saved = JSON.parse(localStorage.getItem('theme-storage') || '{}')
    var id = saved.state && saved.state.currentThemeId
    // 浅色主题 id 白名单（防首帧闪深色）；未命中默认深色
    var lightThemes = ['light', 'ocean-breeze', 'pixel-art', 'moe-soft']
    var isLight = lightThemes.indexOf(id) !== -1
    if (!isLight) document.documentElement.classList.add('dark')
  } catch {
    document.documentElement.classList.add('dark')
  }
})()

