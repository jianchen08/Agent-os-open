---
name: 创建主题
description: 创建或修改前端 UI 主题时加载。主题配置规范：ThemeConfig 结构（colors/components/effects/backgrounds 四大支柱）、命名约定、预设文件 + index.ts 注册流程、validateThemeConfig 校验规则、避开后端孤儿 design_tokens 系统。
---

# 创建主题

## 一、先认清：项目里有两套"主题"，只创建前端这套

| 系统 | 位置 | 状态 | 是否创建 |
|------|------|------|----------|
| **前端主题系统（本规范对象）** | `frontend/src/config/themes/` + `frontend/src/types/theme.ts` | ✅ 实际在用，7 个预设主题，已接入 DOM/shadcn | ✅ 在此创建 |
| 后端 design_tokens（孤儿） | `src/ui_schema/design_tokens.py` + `style_config.py` | ⚠️ 零外部引用，`ThemeName` 仅 light/dark | ❌ 不要碰 |

**判定依据**：后端 `design_tokens.py` 在 `ui_schema/` 之外无任何消费方（grep 验证），前端主题系统才是真实生效的视觉主题来源。本技能只处理前者。

## 二、主题文件放哪、叫什么

| 对象 | 规范 | 示例 |
|------|------|------|
| 主题 ID | `kebab-case`，全局唯一，与文件名一致 | `ocean-breeze` |
| 预设文件 | `frontend/src/config/themes/presets/{id}.ts` | `presets/ocean-breeze.ts` |
| 导出常量 | `camelCase + Theme` | `export const oceanBreezeTheme` |
| 类别 category | `light` / `dark` / `special` / `base` 四选一 | `light` |

## 三、ThemeConfig 四大支柱（结构真相源 = `frontend/src/types/theme.ts`）

类型定义是唯一真相源。创建/修改前务必对照 `types/theme.ts`，不要凭记忆。四大顶层字段：

### 1. colors（必填，决定配色）

```ts
colors: {
  primary:   '#0891b2',          // 主色，必填
  secondary: '#06b6d4',          // 次色，必填
  accent:    '#22d3ee',          // 强调色，必填
  background: { main, card, sidebar, input, elevated },   // 必填，5 个子字段
  text:       { primary, secondary, muted, disabled },     // 必填，4 个子字段
  border:     { default, hover, active },                  // 必填，3 个子字段
  status:     { success, warning, error, info, running, pending }, // 必填，6 个子字段
  bubble:     { user_bg, user_text, ai_bg, ai_text, ...可选 },    // 必填，气泡配色
  // 可选扩展：task / phase / acceptance / task_type / agent_level
}
```

- 颜色值：纯色用 `#RRGGBB`；带透明度用 `rgba(...)`；渐变背景可写 `linear-gradient(...)`
- `background.main` 既可是纯色也可是渐变（会影响 body 背景，见 `themeService.applyTheme`）

### 2. components（必填，决定组件外观）

覆盖全部交互组件样式，**字段较多，照抄一个最接近的预设再改**（推荐抄 `ocean-breeze.ts`）：

| 子字段 | 作用 | 关键约束 |
|--------|------|----------|
| `borderRadius` | 圆角梯度 none/sm/md/lg/xl/full + `defaultRadius` | defaultRadius 必须是六个之一 |
| `fonts` | `ui` / `code` 两个字体族 | code 用等宽字体栈 |
| `fontSize` | xs/sm/md/lg/xl + `defaultFontSize` | 用 rem/px 字符串 |
| `shadows` | none/light/normal/strong 四档 + `defaultShadow` | defaultShadow 是四档之一 |
| `glow` | running/waiting/success/error 发光 + `defaultGlowIntensity` | 可选 |
| `button` | style(pill/square/rounded) + variants(primary/secondary/ghost/destructive) | variants 四套必填 bg/text/border/hoverBg |
| `input` / `card` / `badge` / `dialog` / `tabs` / `toast` / `progress` / `dropdownMenu` | 各组件样式 | 见 types/theme.ts 逐字段 |

### 3. effects（必填，决定动效开关）

```ts
effects: {
  glassmorphism: true,        // 毛玻璃
  animations: true,           // 动画总开关
  transitionDuration: 400,    // ms
  transitionEasing: 'cubic-bezier(0.23, 1, 0.32, 1)',
}
```

### 4. backgrounds（必填，决定区域背景）

```ts
backgrounds: {
  main:    { type: 'gradient'|'solid'|'image', value: '...' }, // 必填
  image:   { enabled, url, position, size, attachment, overlay, overlayOpacity }, // 可选
  texture: { type: 'none'|'dots'|'grid'|'noise'|'lines', ... }, // 可选
  sidebar: { type, value, texture },  // 可选
  chat:    { type, value },           // 可选
  particles / waves / stars / scanlines,  // 可选特效
}
```

## 四、创建流程（TDD-like：先有结构再落文件）

### 1. 选基线主题
挑一个 `category` 最接近的预设，整文件复制改名，**只改差异部分**，不要从零手写（字段太多易漏）。

### 2. 写预设文件
路径 `frontend/src/config/themes/presets/{id}.ts`，导出 `{camelCase}Theme: ThemeConfig`，头部加中文注释说明主题意境与适用场景（参照 `ocean-breeze.ts` 头部）。

### 3. 注册到 index.ts（三处缺一不可）
编辑 `frontend/src/config/themes/index.ts`：
```ts
import { myTheme } from './presets/my-theme'          // ① import
export { myTheme } from './presets/my-theme'          // ② re-export
export const presetThemes = { /* ... */ 'my-theme': myTheme }  // ③ 加入映射表
// 并在 themeList 数组补一条 ThemeInfo（含 preview 五色预览）
```

### 4. 自检结构
对照 `themeService.validateThemeConfig` 的必填校验项逐项核对：
- [ ] `id` / `name` 为非空字符串
- [ ] `colors` 含 primary/secondary/accent + background/text/border 子对象
- [ ] `components` / `effects` / `backgrounds` 三个对象齐全

## 五、命名与配色建议

- ID 见名知意：`{意境}-{明暗}` 或 `{意象}`，如 `forest-dark`、`sakura`
- 同一主题内 `accent` 与 `primary` 形成对比但同色系和谐
- `dark` 类别：背景用深色（`#0xxxxx`），文字浅色；`light` 类别反之
- 状态色 `success/warning/error/info` 跨主题保持语义一致（绿/橙/红/蓝），不要乱改语义
- 无障碍主题（如 `high-contrast`）category 用 `special`，可加 `accessibility: true`

## 六、不要做的事

- ❌ 不要改 `frontend/src/types/theme.ts` 的结构来迁就你的主题——结构是契约，主题适配结构而非反过来
- ❌ 不要在后端 `src/ui_schema/design_tokens.py` / `style_config.py` 建主题——那是未接入的孤儿系统
- ❌ 不要漏 `index.ts` 的三处注册之一（漏了主题不会被 `presetThemes` / 主题选择器发现）
- ❌ 不要给纯色字段填 `linear-gradient()`（仅 `background.main` 等明确支持渐变的字段可以）

## 七、验证清单

创建主题后逐项核对：
- [ ] 预设文件 `frontend/src/config/themes/presets/{id}.ts` 存在，导出 `{camelCase}Theme`
- [ ] `index.ts` 三处注册齐全（import / re-export / presetThemes + themeList）
- [ ] `id` 与文件名一致、全局唯一、kebab-case
- [ ] colors/components/effects/backgrounds 四大顶层字段齐全
- [ ] category 取值合法（light/dark/special/base）
- [ ] 通过 `validateThemeConfig`（必填项无缺失）
- [ ] 前端类型检查通过：在 `frontend/` 下 `npx tsc --noEmit` 无新增报错
- [ ] 配色无硬编码冲突：状态色语义正确、明暗对比可读
