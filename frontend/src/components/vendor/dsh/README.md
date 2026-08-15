# DSH 视觉组件（vendor 移植层）

> task_dsh_plugin_adapter 任务 3。本目录是 DeepSeek Harness (DSH)
> `packages/client/ui-primitives` 组件的**移植副本**，不是运行时依赖。

## 出处与版本锁定

- 源仓库：`D:\reference_repos\deepseek-harness`（只读参考，零改动）
- 锁定 commit：`47f943859bef60e4160492346772ded9b24f765a`（0.1.0-rc.5）
- 许可证：MIT，Copyright (c) 2026 DeepSeek——每个文件头保留出处注释
- DSH 0.1.0-rc 阶段 breaking changes 频繁，升级 = 按本表重移植 + 版本记录

### npm 发布线核查（2026-08-15 实测）

npm registry 已发布 `@deepseek-ai/dsh-client-ui-primitives` /
`dsh-client-ui-tool`，版本 `0.0.1-rc.1`（早于本地锁定的 0.1.0-rc.5，
npm public 刚起步）。实测结论：**npm 包不能直接进灵汐 bundle**——产物只有
`lib/index.js` 单文件 bundle，CSS Modules 被 stub 成空对象（零样式），且
引入 shiki/katex/mdast/anser 重运行时依赖。组件级行为指纹（DiffBlock
maxLines=16、JsonTree 预览限制 4/5/2、TerminalBlock 状态文案）与锁定
commit 源码一致，vendor 锁定仍准确。下载包的 toolview 清单可经
`dsh_translate_manifest` 翻译（translator 支持 lib/*.js 产物扫描）。

## 组件清单与移植状态

| 组件 | 源文件 | 移植方式 | 剥离/替换的依赖 |
|------|--------|----------|----------------|
| DiffBlock | DiffBlock.tsx | 原样 | 无（本就 cordis-free） |
| JsonTree | JsonTree.tsx | 改造 | Menu 右键菜单 + dsh icons → lucide Check/Copy，单击复制默认项 |
| ReadBlock | ReadBlock.tsx | 改造 | shiki 高亮 → 纯文本（原组件对未知语言的合法降级路径），接入点集中在 highlightLines() |
| SearchBlock | SearchBlock.tsx | 原样 | 无 |
| TerminalBlock | TerminalBlock.tsx | 原样 | 无（ansi.ts 一并移植） |
| WebBlock | WebBlock.tsx | 改造 | MarkdownText（mdast 增量渲染）→ 灵汐 MarkdownRenderer |
| CodeBlock | markdown/CodeBlock.tsx | 改造 | shiki → react-syntax-highlighter Prism（灵汐既有依赖） |
| Pill / StateDot | 同名 | 原样 | 无（TerminalBlock 的依赖） |
| clipboard / head-tail-cap / use-copy-feedback / ansi | 同名 | 原样 | 无 |

## 样式与 token

组件 CSS Modules 原样保留，只消费 `--dsw-*` 令牌；令牌由
[`dsh-tokens.css`](./dsh-tokens.css) 重绑到灵汐 design-tokens.css /
theme.css（引用而非求值，`.dark` 主题切换自动跟随）。几何值（字号/行高/
圆角）保留 DSH 原值——DSH 自己规定"字号间距不 token 化"。

## 使用方式

经 `render 意图路由层`（`src/utils/dshRenderIntent.ts` +
`ActivityCard` 的 `dsh:*` content 分支）消费，不直接散引——保证回退级联
（render 声明 → chat_card 声明 → 内置 → 手写 → 推理）的优先级可预期。
