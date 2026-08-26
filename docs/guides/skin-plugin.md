# 皮肤插件开发指南（平台皮肤能力）

> 2026-08-22 裁决：**皮肤能力（CSS 注入 + JS hooks 脚本 + 装饰层渲染）是平台能力**。
> 任何插件都能提供皮肤，不需要 DSH 适配器——DSH 的 16 款皮肤只是这个能力的
> 第一个消费者（适配器在递送层把 DSH 词汇翻译成平台/灵汐词汇）。
> 运行时实现：`frontend/src/services/skinRuntime.ts`；契约蓝本：DSH skin-center
> v2 契约（`plugins/shared/system/dsh_adapter/dsh_plugins/skin-center/contracts/`）。

## 一件皮肤是什么

一个皮肤资产目录（放插件内任意子目录，推荐 `styles/skins/<skin-id>/`）：

```
plugins/shared/<type>/<你的插件>/
├── plugin.json
└── styles/skins/<skin-id>/
    ├── skin.css        # 静态样式（令牌/组件样式；建议全部圈在 html[data-skin="<plugin>:<skin>"] 下）
    ├── hooks.mjs       # 可选：动态脚本（背景图/装饰 DOM/标题栏/光标等）
    └── assets/         # 图片等资产（经插件端点同源递送）
```

`plugin.json` 声明（既有 `contributes.themes` 通道，加一个 `skin` 字段即点亮皮肤能力）：

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

## 运行时做什么（选中该主题的瞬间）

1. **平台 scope 打标**：`<html data-skin="<pluginId>:<skin>">`——你的 CSS/JS 用它
   限定作用域，天然多皮肤隔离、切换即摘。
2. **CSS 按择注入**：拉 `/ext/<pluginId>/styles/skin/<skin>/merged.css`（你把
   skin.css 等合并递送），消毒后注入 `<style>`。
3. **hooks 运行**：拉同路径 `hooks.mjs`，blob 导入执行 `apply(ctx)`。
4. **装饰条槽位**：hooks 建的 fixed 全宽条自动量高让位（`--skin-chrome-top/bottom`）。

切换/取消：摘标记、删 `<style>`、逆序执行 hooks 注册的清理、装饰层清空。

## hooks.mjs 契约（v1alpha1 形态）

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

纪律：**default 导出工厂**（`defineSkinHooks()` 返回 `{apply}`）；模块加载零顶层
副作用；所有 DOM 写入必须经 `ctx.onCleanup` 可回滚；apply 半途抛错运行时会按已
注册清理回滚（静态 CSS 不受影响）。

## 递送端点（插件侧需要的三条路由）

标准路径（前端运行时按此拉取；`{skin}` = 声明的 `skin` 字段值）：

| 路径 | 递送 |
|---|---|
| `GET /ext/<pluginId>/styles/skin/{skin}/merged.css` | 合并后的皮肤 CSS（text/css） |
| `GET /ext/<pluginId>/styles/skin/{skin}/hooks.mjs` | hooks 脚本原文（text/javascript；无则 404，运行时跳过动态层） |
| `GET /ext/<pluginId>/styles/skin-assets/{skin}/{file}` | 资产（图片，按扩展名白名单） |

在 plugin.json 的 `http_endpoints` 声明 + 插件 server 里实现（读皮肤目录文件返回
dispatcher 信封即可）。完整参考实现：`plugins/shared/system/dsh_adapter/server.py`
的 `_serve_merged_skin_css` / `_serve_skin_asset`（含穿越防护与内容类型表）。

## 与 DSH 皮肤的关系

DSH 皮肤（skin-center 16 款）经 dsh_adapter 装载：适配器递送时把 DSH 词汇
（`html[data-dsh-skin]` scope、`[data-pane=*]` 三栏、`[data-slot=*]` 槽位、
camelCase 组件类名等）翻译成平台/灵汐词汇（`data-skin` / `data-region` /
`data-testid` / `data-chat-state`），映射表 `_DSH_POSITION_MAP` 与 CSS/hooks 同源。
自写皮肤直接用平台/灵汐词汇书写，零翻译、零适配器。
