# NOTICE —《高性价比人生指南》vendor 声明

## 内容来源

- 上游仓库：github.com/eternity4719/HowToLiveBetter（HowToLiveBetter by eternity4719）
- vendor 范围：`book/` 下 34 章正文 markdown（772 条循证生活建议）
- vendor 日期：2026-11-19（上游 main 分支浅克隆）

## 许可

- 书正文（`book/` 全部文件）：**CC BY 4.0**（Creative Commons Attribution 4.0 International）
  - 许可全文：https://creativecommons.org/licenses/by/4.0/
  - 上游仓库内的许可正本：上游根目录 `LICENSE` 文件
- 上游仓库的代码类文件（脚本等）为 MIT 许可（上游 `LICENSE-CODE`），本包未 vendor 任何上游代码文件。

## 署名（Attribution）

本包 `book/` 内容来自 eternity4719 的《高性价比人生指南》，
原文见 https://github.com/eternity4719/HowToLiveBetter 。

## 改动声明（Changes）

- `book/` 34 章正文：**原样 vendor，零改动**（未修改、未删减、未蒸馏）。
- 本包新增的 `SKILL.md`（agent 使用规则）基于上游
  `skills/life-decision-guide/SKILL.md` **改编**：保留其第 0-5 步流程，
  改写取文路径（hindsight recall 知识库主路 + 包内 vendor 兜底），
  并追加 AgentOS 个人助理模式本地化规则三条（pim 固化 / 主动时刻 /
  一锚一次）。SKILL.md 是改编件，不代表上游原文。
- `NOTICE.md` 本身为本包新增，上游无此文件。

## 上游更新

同步入口 = 仓内 `scripts/sync_life_guide.py`（知识库主路）；
vendor 兜底副本的手工更新 = 重新浅克隆上游后按上游 `book/` 整目录覆盖
（覆盖仍须保持正文零改动，本文件同步更新 vendor 日期）。
