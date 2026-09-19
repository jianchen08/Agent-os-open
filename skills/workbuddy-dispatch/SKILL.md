---
name: WorkBuddy 发任务
description: WorkBuddy 桌面端的应用档案（窗口匹配/输入定位/发送动作/完成信号/结果契约）。配合额度代理方法论把任务派发给 WorkBuddy、用其额度执行并收割结果。需要驱动 WorkBuddy 时加载。
---

# WorkBuddy 应用档案

> 派发前先加载方法论：`skills/credit-delegation/SKILL.md`（接口优先级、三阶段协议、token 经济学）。本文件只记录 WorkBuddy 的五要素。工具用 computer_use 上游原名：App/Snapshot/Click/Type/Shortcut/Wait。

## 五要素

| 要素 | 内容 |
|---|---|
| 窗口匹配 | 窗口清单含 "WorkBuddy"（**首跑校准**：本机未实测，以实际窗口清单为准） |
| 输入定位 | Snapshot 找输入框元素 label；无 label 则实测坐标（沉淀回本表） |
| 发送动作 | 点"发送"按钮；无按钮再按 Enter（首跑确认是否 Enter=发送） |
| 完成信号 | 首选结果文件（简报约定写入 %USERPROFILE%\agentos_dispatch\workbuddy-<任务名>.md）；备用：任务卡状态变化 |
| 结果契约 | 简报末尾约定结果落盘路径（同上） |

## 实测怪癖

- 待首跑校准后回填。
