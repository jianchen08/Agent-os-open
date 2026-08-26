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

## 3. 皮肤（skin）——CSS 注入 + hooks 装饰层

> 2026-08-22 裁决：**皮肤能力是平台能力**——任何插件都能提供皮肤；DSH 的 16 款皮肤只是第一个消费者（适配器在递送层做词汇翻译）。运行时实现：`frontend/src/services/skinRuntime.ts`。

`contributes.themes[].skin` 字段点亮皮肤能力（在既有主题声明上加一个 `skin` 字段）。

### 3.1 一件皮肤是什么

一个皮肤资产目录（放插件内任意子目录，推荐 `styles/skins/<skin-id>/`）：

```
plugins/shared/<type>/<你的插件>/
├── plugin.json
└── styles/skins/<skin-id>/
    ├── skin.css        # 静态样式（建议全部圈在 html[data-skin="<plugin>:<skin>"] 下）
    ├── hooks.mjs       # 可选：动态脚本（背景图/装饰 DOM/标题栏/光标等）
    └── assets/         # 图片等资产（经插件端点同源递送）
```

```json
{
  "contributes": {
    "themes": [
      {
        "id": "my-skin-a",
        "name": "自有皮肤 A",
        "base": "dark",
        "skin": "a",
        "variables": { "--ds-accent-primary": "#ff6f00" }
      }
    ]
  }
}
```

- `id`：主题 ID（主题卡显示）；`skin`：皮肤资产 ID（端点路径段）。
- `base`：亮/暗基准；暗色时运行时自动打 `body[data-skin-dark]`（皮肤 CSS 可用它做暗色变体）。
- `variables` / `backgrounds`：走既有主题管线（变量 setProperty / 原生背景图层）。

### 3.2 运行时做什么（选中该主题的瞬间）

1. **平台 scope 打标**：`<html data-skin="<pluginId>:<skin>">`——CSS/JS 用它限定作用域，天然多皮肤隔离、切换即摘。
2. **CSS 按择注入**：拉 `/ext/<pluginId>/styles/skin/<skin>/merged.css`（皮肤 CSS 合并递送），消毒后注入 `<style>`。
3. **hooks 运行**：拉同路径 `hooks.mjs`，blob 导入执行 `apply(ctx)`。
4. **装饰条槽位**：hooks 建的 fixed 全宽条自动量高让位（`--skin-chrome-top/bottom`）。

切换/取消：摘标记、删 `<style>`、逆序执行 hooks 注册的清理、装饰层清空。

### 3.3 hooks.mjs 契约（v1alpha1）

```js
export default function defineSkinHooks() {
  return {
    apply(ctx) {
      // ctx.skinId     皮肤资产 ID（"a"）
      // ctx.scopeAttr  scope 属性值（"my_plugin:a"）——拼选择器：
      //                html[data-skin="${ctx.scopeAttr}"]；不要自己写属性
      // ctx.assetBase  同源资产前缀（"/ext/my_plugin/styles/skin-assets/a/"）
      // ctx.layers     六个装饰层 {background,ambient,top,bottom,sidebar,foreground}
      //                全部 pointer-events:none、层序固定——装饰 DOM 往里挂
      // ctx.theme.get() 'light' | 'dark'
      // ctx.onCleanup(fn) 注销回调（幂等，可注册多条）
      const bg = document.createElement('div')
      bg.style.cssText = 'position:absolute;inset:0;background:center/cover no-repeat'
      bg.style.backgroundImage = `url(${ctx.assetBase}assets/bg.webp)`
      ctx.layers.background.appendChild(bg)
      ctx.onCleanup(() => bg.remove())
    },
  }
}
```

纪律：**default 导出工厂**（`defineSkinHooks()` 返回 `{apply}`）；模块加载零顶层副作用；所有 DOM 写入必须经 `ctx.onCleanup` 可回滚；apply 半途抛错运行时按已注册清理回滚（静态 CSS 不受影响）。

### 3.4 递送端点（插件侧三条路由）

| 路径 | 递送 |
|---|---|
| `GET /ext/<pluginId>/styles/skin/{skin}/merged.css` | 合并后的皮肤 CSS（text/css） |
| `GET /ext/<pluginId>/styles/skin/{skin}/hooks.mjs` | hooks 脚本原文（text/javascript；无则 404，运行时跳过动态层） |
| `GET /ext/<pluginId>/styles/skin-assets/{skin}/{file}` | 资产（图片，按扩展名白名单） |

在 plugin.json 的 `http_endpoints` 声明 + 插件 server 里实现（读皮肤目录文件返回 dispatcher 信封即可）。完整参考实现：`plugins/shared/system/dsh_adapter/server.py` 的 `_serve_merged_skin_css` / `_serve_skin_asset`（含穿越防护与内容类型表）。

### 3.5 与 DSH 皮肤的关系

DSH 皮肤（skin-center 16 款）经 dsh_adapter 装载：适配器递送时把 DSH 词汇（`html[data-dsh-skin]` scope、`[data-pane=*]` 三栏、camelCase 组件类名等）翻译成平台/灵汐词汇（`data-skin` / `data-region` / `data-testid` / `data-chat-state`）。自写皮肤直接用平台/灵汐词汇书写，零翻译、零适配器。
