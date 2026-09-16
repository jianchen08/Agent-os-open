# 主题规则（Theme）

> 返回 [开发指南索引](README.md)。横切协议规则（供注入遵守）：主题与皮肤的权威契约。
> 模式/角色私有主题（卡内 theme 段、动态档）权威载体在模式包物料，本篇只写跨模式协议约束。
> 已落地不标；（规划中 Wave 2）= 已定稿未建成，不得按其编写集成。

## 一、红线（不可违反）

1. **插件永不直接注入 CSS/DOM**：插件代码禁止操作页面样式或 DOM；一切视觉差异走声明面（`contributes.themes`）或平台白名单方法（`theme.apply`，规划中 Wave 2）。
2. 主题应用只经平台运行时：预设编译、contributes 变量覆盖、皮肤资源消毒注入都由平台执行——插件只声明，不执行。
3. 无障碍硬性要求：配色过 `validateThemeConfig` 校验；文本/背景对比度达无障碍门槛（high-contrast 预设即 WCAG 2.1 AAA 基准）；文字颜色与背景择优取黑/白，禁止低对比撞色。

## 二、现行两条轨（已落地）

### 2.1 前端预设（主轨）

- 形态：`frontend/src/config/themes/presets/*.ts` 导出 `ThemeConfig`（类型真值源 `frontend/src/types/theme.ts`；四支柱 colors/components/effects/backgrounds）。
- 注册三步：新建预设文件 → `frontend/src/config/themes/index.ts` 的 `presetThemes` 映射 → `themeList` 补一条 `ThemeInfo`（含 `preview` 五色预览）；注册后自动出现在主题设置页，无需其它登记。
- 结构样例照抄 `presets/moe-soft.ts`（现 7 预设：dark/light/deep-space/ocean-breeze/high-contrast/pixel-art/moe-soft）。

### 2.2 插件主题（辅轨，contributes.themes）

- 任何插件可在 manifest 声明主题包——纯 CSS 变量键值对，无 JS 执行：`{id, name, base(dark/light 打底), variables(CSS 变量覆盖，后写者胜), backgrounds}`；随插件启用自动出现在主题列表。批量样例 `plugins/shared/system/dsh_adapter/plugin.json`（16 款）。
- 皮肤（`themes[].skin`）：皮肤资产目录（推荐 `styles/skins/<skin-id>/`：`skin.css` 全部圈在 `html[data-skin="<plugin>:<skin>"]` 下；可选 `hooks.mjs`；`assets/` 经插件端点同源递送）。hooks 契约：default 导出工厂 `defineSkinHooks()` 返回 `{apply(ctx)}`；模块零顶层副作用；所有 DOM 写入经 `ctx.onCleanup` 可回滚，apply 抛错按已注册清理回滚。
- 皮肤运行时（平台，`frontend/src/services/skinRuntime.ts`）：scope 打标 → 拉 `/ext/<pluginId>/styles/skin/<skin>/merged.css` 消毒注入 → 执行 `hooks.mjs` → 切换即摘（标记/style/hooks 清理逆序）。递送三路由：`merged.css` / `hooks.mjs` / `skin-assets/{file}`（http_endpoints 声明 + 读文件返回信封；参考 `dsh_adapter/server.py`）。
- 补充通道：动态 JSON 主题 `frontend/public/themes/*.json`（构建期扫描）；用户自定义存浏览器 localStorage（隐私模式/清缓存/换设备丢失，恢复默认 = 选 dark 主题 + dark 模式）。

用户侧操作：设置页 `/settings/theme` 切显示模式（light/dark/system）与主题卡；顶部 `ThemeButton` 快捷面板。主题选择随切换立即生效。

## 三、动态主题协议（规划中 Wave 2）

- **唯一通道 = `theme.apply` 白名单方法**：载荷 = 结构化 ThemeConfig 档（schema 校验），禁止裸 CSS/选择器/任意 JS。
- **会话 override 栈**：push/pop 可逆；静态层 = 物料自带 theme 段（切角色 = 切档）；动态层 = day/night 等变体经 rules 映射。
- **卡内 theme 段**：模式物料（如角色卡）内嵌 ThemeConfig（待拍板：卡内嵌 vs 独立档，建议卡内嵌；作用域建议仅聊天区 + 面板）。`state.character` 类模式键为派生观测标签（装饰组件读），不做第二身份机制。
- 分界句式：差异能否用「换一个物料键」表达？能 = 输入（theme 段）；需要新渲染能力 = 补通用宿主件（一次性）+ 包内声明，禁止为某模式改 `frontend/src`。
