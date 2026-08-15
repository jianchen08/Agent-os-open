PROVENANCE
本目录存放所有经 dsh_adapter 装载的 DSH 插件包（npm 下载物原样保留）。

- ui-primitives/  @deepseek-ai/dsh-client-ui-primitives 0.0.1-rc.1（纯组件库，非 client 插件）
- ui-tool/        @deepseek-ai/dsh-client-ui-tool 0.0.1-rc.1（client 插件，toolview 键 = render 意图键域）

来源：npm registry（registry.npmjs.org），MIT License (c) 2026 DeepSeek。
装载方式：server.py 启动时扫描本目录 → translator.translate_package 批量翻译 →
dsh_list_plugins 汇报。新增 DSH 插件 = 放一个子目录进来（npm 解压物或源码检出），
适配器自动发现，无需改适配器代码。
