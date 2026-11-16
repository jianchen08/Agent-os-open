# 上游来源（provenance）

- 仓库：https://github.com/cathrynlavery/diagram-design（Editorial diagram design skill）
- 版本：v2.6（SKILL.md frontmatter `metadata.version`）
- 安装快照 commit：f4547ee95f88e5b28a52517feff6b6c11cc657f9（2026-10-08）
- 许可：MIT
- 安装方式：从 raw.githubusercontent.com 按 git tree 逐文件下载 `skills/diagram-design/` 全量（279 个文件）到本目录（ZCode 用户级技能目录）。

## 与上游包的差异（本地新增）

- `scripts/verify-geometry.py`、`scripts/verify-motion.py`：上游放在仓库根 `scripts/`（CI 用），
  未随技能目录分发；SKILL.md 检查单里以 `<repo-root>/scripts/...` 占位引用。本地补入
  （纯标准库实现），使几何/动效校验在已安装技能下也可直接运行。
- `references/` 文档中提及的其余仓库根脚本（verify-*/lint-*/build-*/test-verify-*）属上游
  CI 体系，按上游打包惯例不随技能分发，未带入。

## 更新方法

对照上游 `skills/diagram-design/` 的 git tree 重新逐文件下载覆盖本目录，并核对本文件
记录的 commit 是否前进；上游版本号见 SKILL.md frontmatter。
