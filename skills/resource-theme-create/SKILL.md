---
name: 创建主题
description: 创建或修改前端 UI 主题时加载。主题配置规范：ThemeConfig 结构（colors/components/effects/backgrounds 四大支柱）、命名约定、预设文件 + index.ts 注册流程、validateThemeConfig 校验规则、配色对比度/无障碍/美观设计要求。主题由配置自动读取，无需注册、无需资源搜索。
---

# 创建主题

## 〇、定位：主题是"配置自动读取"资源，不注册、不搜索

| 问题 | 答案 | 原因 |
|------|------|------|
| 主题要不要 `register_resource` 注册？ | **不要** | 前端 `index.ts` 的 `presetThemes` 映射表即注册中心，放进去就自动被发现 |
| 主题要不要 `resource_search` 搜索？ | **不要** | 主题不是后端 Registry 资源，搜索工具只管 agent/tool/skill |
| 谁用这个技能？ | **资源管理 Agent** | 在用户要新增/改主题时加载本技能执行 |

> **不要改 `register_resource` / `resource_search` 去加 theme 类型。** 那是给后端动态资源用的通道，主题走配置直读，两套机制不该混。

## 一、唯一主题系统在前端（后端那套已清理）

| 系统 | 位置 | 状态 |
|------|------|------|
| **前端主题系统（本规范对象）** | `frontend/src/config/themes/` + `frontend/src/types/theme.ts` | ✅ 实际在用，7 个预设主题，接入 DOM/shadcn |

> 历史上后端曾有一套 `design_tokens` / `style_config`，零消费方，已删除。**主题只在前端做。**

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

## 五、配色：对比度 / 无障碍 / 美观（核心质量门禁）

颜色是主题的灵魂，也是最易翻车的地方。下面是硬性门禁，**任一不过即主题不合格**。

### 5.1 对比度门禁（WCAG 2.1，必须自检）

文字色与其背景色的对比度（contrast ratio）必须达标。计算公式：取两色相对亮度，`(L1+0.05)/(L2+0.05)`，范围 1~21（写法 `4.5:1`）。

| 元素 | 文本规模 | AA（常规目标） | AAA（高标准，参考 `high-contrast` 主题） |
|------|----------|----------------|------|
| **正文文字**（`text.primary` on `background.main`） | 正常 < 18px | ≥ 4.5:1 | ≥ 7:1 |
| **大号文字/标题** | ≥ 18px 或 ≥ 14px 粗体 | ≥ 3:1 | ≥ 4.5:1 |
| **次要文字**（`text.secondary` on bg） | — | ≥ 4.5:1（同正文） | ≥ 7:1 |
| **辅助/静默文字**（`text.muted` on bg） | — | ≥ 3:1 | ≥ 4.5:1 |
| **状态色文字**（success/warning/error on bg） | — | ≥ 4.5:1 | ≥ 7:1 |
| **图标/边框/占位**（`border.default`/`text.disabled` on bg） | 非文字 | ≥ 3:1（可见即可） | ≥ 3:1 |

**自检方法**：对每对"前景 on 背景"，用对比度工具核对。可用：
- 在线：WebAIM Contrast Checker
- 命令行：本仓库可写一个一次性校验脚本，逐对算 ratio 并断言阈值（创建主题时建议附带跑一遍）

> 关键配对要逐一查（不要只查 `primary on bg`）：`text.primary/secondary/muted/disabled` on `background.main`，`bubble.user_text on bubble.user_bg`，`bubble.ai_text on bubble.ai_bg`，各 `status.*` on bg。

### 5.2 明暗一致性（不要做"半暗半亮"主题）

- `category: 'dark'` → 所有背景字段（`main/card/sidebar/input/elevated`）用深色（如 `#0xxxxx` 系），文字用浅色（接近白）
- `category: 'light'` → 所有背景用浅色（接近白），文字用深色（如 `#1xxxxx` 系）
- **层级关系**：`main` 最深/最浅（基调） → `card`/`elevated` 提亮/压暗一档 → `input` 介于二者之间。层次不能塌成一片
- dark 主题里 `text.disabled`（如 `#808080`）on 深色背景对比度常不够，注意往浅调

### 5.3 色彩美观与和谐

- **同色系和谐**：`primary`/`secondary`/`accent` 取同色相不同明度/饱和度，避免三色互相打架。可用 HSL 微调：固定 H，调 S/L
- **主次分明**：`primary` 是绝对主角，`secondary`/`accent` 仅作点缀（按钮次要态、强调高亮），面积上 primary 占主导
- **状态色语义固定，不要改语义**：`success=绿` `warning=橙/黄` `error=红` `info=蓝`，跨主题保持语义一致，只调明暗适配底色
- **渐变要协调**：`background.main` 或 `bubble.*_bg` 用 `linear-gradient` 时，取同色系两端，避免彩虹
- **避免荧光色大面积铺**：纯 `#00ff00`/`#ff00ff` 等饱和原色只适合 `high-contrast` 这类特殊主题，常规主题应降饱和（如 `#059669` 而非 `#00ff00`）

### 5.4 无障碍专项（参考 `high-contrast` 主题）

| 场景 | 要求 |
|------|------|
| 常规主题 | 至少达 **AA**；交互态（hover/active/focus）额外可见 |
| 无障碍主题（`category: 'special'` + `accessibility: true`） | 达 **AAA**；圆角可归零（`high-contrast` 把 borderRadius 全设 0）；加 `accessibility_config`（contrastRatio/focusIndicator/reducedMotion/largeText） |
| 焦点可见性 | 输入框 `input.focusBorder` + `focusGlow` 要与背景对比度 ≥ 3:1，键盘用户能看清聚焦位置 |
| 动效偏好 | 系统级 `prefers-reduced-motion` 由前端 store 处理；主题的 `effects.animations` 不要挡这条路径 |

## 六、不要做的事

- ❌ 不要改 `frontend/src/types/theme.ts` 的结构来迁就你的主题——结构是契约，主题适配结构而非反过来
- ❌ 不要在后端 `src/ui_schema/design_tokens.py` / `style_config.py` 建主题——那是未接入的孤儿系统
- ❌ 不要漏 `index.ts` 的三处注册之一（漏了主题不会被 `presetThemes` / 主题选择器发现）
- ❌ 不要给纯色字段填 `linear-gradient()`（仅 `background.main` 等明确支持渐变的字段可以）

## 七、验证清单

创建主题后逐项核对：
- [ ] 预设文件 `frontend/src/config/themes/presets/{id}.ts` 存在，导出 `{camelCase}Theme`
- [ ] `index.ts` 三处注册齐全（import / re-export / presetThemes + themeList）—— **不需要 register_resource / resource_search**
- [ ] `id` 与文件名一致、全局唯一、kebab-case
- [ ] colors/components/effects/backgrounds 四大顶层字段齐全
- [ ] category 取值合法（light/dark/special/base），明暗一致（dark 全深色/light 全浅色）
- [ ] 通过 `validateThemeConfig`（必填项无缺失）
- [ ] **对比度自检**：正文 ≥ 4.5:1（AA），大号/图标 ≥ 3:1；无障碍主题 ≥ 7:1（AAA）。逐对核查 text/bubble/status on 各 background
- [ ] 配色美观：primary 主导、同色系和谐、状态色语义不乱
- [ ] 前端类型检查通过：在 `frontend/` 下 `npx tsc --noEmit` 无新增报错
