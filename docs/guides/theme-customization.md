# 主题使用指南

本指南介绍系统真实提供的主题功能：**预设主题切换**与**显示模式切换**。

主题系统完全前端化，无后端依赖，配置存储在浏览器 localStorage 中。

---

## 1. 显示模式

系统支持三种显示模式：

| 模式 | 行为 |
|------|------|
| **浅色（light）** | 固定使用浅色主题，适合日间 |
| **深色（dark）** | 固定使用深色主题，适合夜间 |
| **跟随系统（system）** | 根据操作系统设置自动切换浅色/深色 |

**操作方式**：

进入「设置」→「主题设置」（`/settings/theme`），在「显示模式」区域点击对应按钮即可切换。

切换模式后，当前主题会同步更新为对应方向的预设主题（例如选择浅色模式会切到浅色预设主题）。

---

## 2. 预设主题

系统内置 5 套预设主题：

| 主题 ID | 名称 | 类别 | 说明 |
|---------|------|------|------|
| `dark` | 深色主题 | 深色 | 默认深色主题，适合夜间使用 |
| `light` | 浅色主题 | 浅色 | 默认浅色主题，适合日间使用 |
| `deep-space` | 深空指挥台 | 深色 | 模拟太空指挥中心的科技感界面 |
| `ocean-breeze` | 海洋微风 | 浅色 | 清新海洋风配色 |
| `high-contrast` | 高对比度 | 特殊 | 无障碍高对比度主题 |

**操作方式**：

在「主题设置」页面（`/settings/theme`）的「选择主题」区域，点击任意主题卡片即可应用。每张卡片展示该主题的主色、背景色、表面色、强调色预览，以及主题名称与类别标签。

---

## 3. 顶部快捷切换

除设置页面外，顶部导航栏提供主题按钮（`ThemeButton`，图标随当前模式变化），点击后弹出主题面板（`ThemePanel`），可快速在浅色/深色/跟随系统间切换。

---

## 4. 主题配置存储

| 项目 | 说明 |
|------|------|
| 存储位置 | 浏览器 localStorage |
| 持久化 | 切换后立即生效，刷新页面后保留 |
| 丢失场景 | 隐私/无痕模式、清除浏览器缓存、更换浏览器或设备 |

> **注意**：隐私模式下主题设置可能不会被保存。清除浏览器缓存或更换设备后，主题会恢复为默认的深色主题（`dark`）。

---

## 5. 恢复默认主题

1. 进入「设置」→「主题设置」
2. 在「选择主题」中选择「深色主题」
3. 在「显示模式」中选择「深色」

或使用主题面板的快捷切换恢复到深色模式。

---

## 6. 开发者参考

若需要修改预设主题配色或新增预设主题，相关代码与配置：

| 资源 | 路径 | 用途 |
|------|------|------|
| 主题设置页面 | `frontend/src/pages/settings/ThemeSettingsPage.tsx` | 主题选择 UI |
| 预设主题定义 | `frontend/src/config/themes/presets/*.ts` | 各主题的颜色/组件样式配置 |
| 预设主题注册 | `frontend/src/config/themes/index.ts` | `presetThemes` 映射表与 `themeList` 列表 |
| 主题状态管理 | `frontend/src/stores/themeStore.ts` | `useThemeStore`（setTheme/setMode/currentThemeId/resolvedTheme） |
| 主题类型定义 | `frontend/src/types/theme.ts` | `ThemeConfig`/`ThemeInfo`/`ThemeMode` 等类型 |
| 主题服务 | `frontend/src/services/themeService.ts` | 主题编译（`compileThemeVariables` 将配置转为 CSS 变量） |
| 主题按钮/面板 | `frontend/src/components/layout/ThemeButton.tsx`、`ThemePanel.tsx` | 顶部快捷切换组件 |

新增预设主题的步骤：

1. 在 `frontend/src/config/themes/presets/` 下新建主题配置文件（导出符合 `ThemeConfig` 类型的对象）
2. 在 `frontend/src/config/themes/index.ts` 的 `presetThemes` 和 `themeList` 中注册
3. 该主题会自动出现在主题设置页面的选择列表中

主题配置的完整结构（颜色、组件样式、背景、无障碍等字段）见 `frontend/src/types/theme.ts` 中的 `ThemeConfig` 接口定义。
