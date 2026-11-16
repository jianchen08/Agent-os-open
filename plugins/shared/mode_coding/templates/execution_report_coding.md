{{path:config/templates/execution_report_template.md}}

## 上级消费指引（域：编码）

> 本节随执行报告交付上级（主 agent 或编排者），是『风险分级标签』章节的消费规则。

- 报告『风险分级标签』为 independent_review_needed=true 时：上级必须在向用户通知任务完成前，派发 mode_coding/code_reviewer_agent 独立审查（goal 写明待审查的文件/模块范围与审查关注点），并按审查报告裁决通过/回退；裁决回退则退回本执行者修订后重走独立审查。
