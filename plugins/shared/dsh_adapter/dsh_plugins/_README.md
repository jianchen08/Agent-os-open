PROVENANCE
本目录存放所有经 dsh_adapter 装载的 DSH 插件包（npm 下载物原样保留）。

官方工具包（registry.npmjs.org，MIT License (c) 2026 DeepSeek）：
- dsh-tool-calculator/  @deepseek-ai/dsh-tool-calculator 0.0.1
- dsh-tool-csv/         @deepseek-ai/dsh-tool-csv 0.0.1
- dsh-tool-diff/        @deepseek-ai/dsh-tool-diff 0.0.1
- dsh-tool-json/        @deepseek-ai/dsh-tool-json 0.0.1
- dsh-tool-time/        @deepseek-ai/dsh-tool-time 0.0.1

第三方包：
- modlens/          @liustack/modlens 3.17.3（settings.plugin.item 设置面板视觉包）
- skin-center/      @linxin666/dsh-client-ui-skin-center 0.2.5（DSH Web UI 皮肤中心：
                    内置 15 套皮肤资产 xp/matrix/miku/dragon-heir/maid-atelier 等，
                    GUI 切换、同一时刻仅一套激活；Apache-2.0，**例外：maid-atelier
                    皮肤 CC BY-NC-SA 4.0 非商业许可**。源仓库
                    github.com/zhu1090093659/dsh-web-ui ★5k+，v1「一皮肤一包」形态
                    已被上游退役合并进本包）

官方视觉包（2026-08-21 装，MIT）：
- ui-theme/              @deepseek-ai/dsh-client-ui-theme 0.0.1-rc.1（主题机制插件：
                        ThemeService light/dark/system + --dsw-* token 样式 + Appearance
                        设置行；settings.general.item → settingsPanels）
- ui-brand-official/     @deepseek-ai/dsh-client-ui-brand-official 0.1.0-rc.8（官方品牌
                        视觉：sidebar.brand.* / conversation.hero.brand.* 槽位占位，
                        与 vendor 基线 rc.8 同版）

装载方式：server.py 启动时扫描本目录 → translator.translate_package 批量翻译 →
dsh_list_plugins 汇报。新增 DSH 插件 = 放一个子目录进来（npm 解压物或源码检出），
适配器自动发现，无需改适配器代码。视觉包翻译产物是槽位语义映射（未收录槽位
→ direct 直接渲染诚实边界）；组件本体不在翻译范围（vendor 移植层另行覆盖）。
