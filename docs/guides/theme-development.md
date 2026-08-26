# 主题开发

> 返回 [开发指南索引](README.md)。使用侧（怎么切换主题）见 [theme-customization.md](theme-customization.md)。

主题有**两条轨**，按需求选：

| 轨 | 形态 | 适合 |
|---|---|---|
| 前端预设（主轨） | `frontend/src/config/themes/presets/*.ts` 导出 `ThemeConfig` | 平台内置主题，与插件体系无关 |
| 插件主题（辅轨） | manifest `contributes.themes`（CSS 变量包，可选 skin 皮肤） | 随插件分发的主题/皮肤，热插拔 |

另有两条补充通道：动态 JSON 主题放 `frontend/public/themes/*.json`（构建期扫描）；用户自定义主题存浏览器 localStorage。

## 1. 前端预设主题

**四支柱结构**（类型真值源 `frontend/src/types/theme.ts`）：

```ts
export const myTheme: ThemeConfig = {
  id: 'my-theme',
  name: '主题名',
  description: '一句话描述',
  category: 'light',            // light / dark / special
  colors: {                     // 支柱一：颜色
    primary: '#d9738f', secondary: '#b8a1cf', accent: '#f5d6b8',
    background: { main: '...', card: '...', sidebar: '...', input: '...', elevated: '...' },
    text: { primary: '...', secondary: '...', muted: '...', disabled: '...' },
    border: { default: '...', hover: '...', active: '...' },
    status: { success: '...', warning: '...', error: '...', info: '...', running: '...', pending: '...' },
    bubble: { user_bg: '...', user_text: '...', user_radius: '...', ai_bg: '...', ai_text: '...', ai_radius: '...' },
  },
  components: { ... },          // 支柱二：圆角/字体/字号/阴影/按钮/输入框/卡片等
  effects: { glassmorphism: true, animations: true, transitionDuration: 200, ... },  // 支柱三
  backgrounds: { main: { type: 'solid', value: '#fff9f5' }, ... },                    // 支柱四
}
```

完整真实示例照抄结构：`frontend/src/config/themes/presets/moe-soft.ts`（现共 7 个预设：dark / light / deep-space / ocean-breeze / high-contrast / pixel-art / moe-soft）。

**注册三步**（`frontend/src/config/themes/index.ts`）：import 新预设 → 加入 `presetThemes` 映射表 → 在 `themeList` 补一条 `ThemeInfo`（含 `preview` 五色预览，供设置页展示）。注册后自动出现在主题设置页，无需其它登记。

**硬性要求**：配色过 `validateThemeConfig` 校验；文本/背景对比度须达无障碍门槛（high-contrast 预设即 WCAG 2.1 AAA 基准）；文字颜色与背景择优取黑/白，禁止低对比撞色。

## 2. 插件主题（contributes.themes）

任何插件可在 manifest 声明主题包——纯 CSS 变量键值对，无 JS 执行。真实示例 `plugins/shared/system/visual_customization_demo/plugin.json`：

```jsonc
"contributes": {
  "themes": [
    {
      "id": "gold-lace",
      "name": "金色蕾丝",
      "base": "dark",                     // 打底预设：dark / light
      "variables": {                      // CSS 变量覆盖（后写者胜）
        "--ds-accent-primary": "#D4AF37",
        "--ds-bg-canvas": "#17120A",
        "--ds-bg-panel": "rgba(40, 35, 20, 0.92)",
        "--ds-text-primary": "#F5EECB",
        "--ds-border-active": "#D4AF37",
        "--btn-primary-bg": "#B8860B"
      },
      "backgrounds": { "image": {"enabled": false}, "texture": {"enabled": false} }
    }
  ]
}
```

机制：前端经内核聚合出口 `plugin_contributes` 发现插件主题 → 应用时先取 `base` 指定的内置预设打底，再逐个 `setProperty` 覆盖 variables。批量真实示例见 `plugins/shared/system/dsh_adapter/plugin.json`（16 款皮肤主题）。

## 3. 皮肤（skin）

`contributes.themes[].skin` 字段进一步激活皮肤能力：CSS 注入 + `hooks.mjs` 装饰层，样式经 `/ext/{pluginId}/styles/skin/...` 三条端点递送。开发规范见 [skin-plugin.md](skin-plugin.md)（本目录），运行时实现 `frontend/src/services/skinRuntime.ts`。
