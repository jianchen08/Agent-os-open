#!/usr/bin/env bash
#
# md_toc_generator.sh — Markdown 文档目录自动生成器
#
# 功能：
#   扫描 Markdown 文件中所有 ## 二级标题，
#   在文件开头插入 "## 目录" 段落，并附带 GitHub 风格锚点链接。
#
# 用法：
#   ./md_toc_generator.sh <markdown_file>
#
# 特性：
#   - 锚点规则：转小写 → 去除中英文标点 → 空格转 - → 合并连续 - → 去首尾 -
#   - 已存在 "## 目录" 段时自动替换，避免重复插入
#   - 未传参数时打印使用说明并以非零状态退出
#   - 严格模式（set -euo pipefail）保证脚本稳健

set -euo pipefail

# ---------- 配置 ----------
readonly TOC_TITLE="## 目录"

# ---------- 工具函数 ----------

# 打印使用说明
usage() {
  cat <<EOF
用法: $(basename "$0") <markdown_file>

参数:
  markdown_file    要生成目录的 Markdown 文件路径

示例:
  $(basename "$0") docs/sample.md
EOF
}

# 将标题文本转换为 GitHub 风格锚点
# 规则：
#   1. 字母转小写
#   2. 去除中英文标点符号（保留 CJK 字符、字母、数字）
#   3. 空白字符替换为 -
#   4. 合并连续 -
#   5. 去除首尾 -
make_anchor() {
  local title="$1"
  echo "$title" \
    | tr '[:upper:]' '[:lower:]' \
    | sed -E 's/[[:punct:]]//g' \
    | sed -E 's/[[:space:]]+/-/g' \
    | sed -E 's/-+/-/g; s/^-+|-+$//g'
}

# ---------- 主流程 ----------
main() {
  # 1. 参数校验
  if [[ $# -lt 1 ]]; then
    echo "错误: 未指定 Markdown 文件" >&2
    usage >&2
    exit 1
  fi

  local file="$1"

  if [[ ! -f "$file" ]]; then
    echo "错误: 文件不存在: $file" >&2
    exit 1
  fi

  # 2. 提取所有 ## 二级标题（排除目录标题自身）
  #    输出格式: "行号:## 标题"
  local headings
  headings=$(grep -n "^## " "$file" | grep -v ":${TOC_TITLE}$" || true)

  if [[ -z "$headings" ]]; then
    echo "提示: 未找到任何二级标题（## xxx），无需生成目录"
    exit 0
  fi

  # 3. 生成目录 Markdown 内容
  local toc="$TOC_TITLE"
  while IFS= read -r line; do
    # line 形如 "12:## 项目背景"
    local heading
    heading=$(echo "$line" | sed -E 's/^[0-9]+:## //')
    local anchor
    anchor=$(make_anchor "$heading")
    toc+=$'\n'"- [$heading](#$anchor)"
  done <<< "$headings"

  # 4. 重组文件：替换已有目录段 或 在文件开头插入
  local outfile
  outfile=$(mktemp)

  # 查找 "## 目录" 段的起始行（1-based）
  local toc_start
  toc_start=$(grep -n "^${TOC_TITLE}$" "$file" | head -n1 | cut -d: -f1)

  if [[ -n "$toc_start" ]]; then
    echo "检测到已有目录段（第 ${toc_start} 行），将进行替换..."

    # 找到目录段的结束行：下一个 ## 标题行 之前一行；若无则为文件末尾
    local toc_end
    toc_end=$(awk -v start="$toc_start" '
      NR > start && /^## / { print NR - 1; exit }
      END { print NR }
    ' "$file")

    # 重组：目录段之前 + 新目录 + 目录段之后
    {
      sed -n "1,$((toc_start - 1))p" "$file"
      printf "%s\n" "$toc"
      sed -n "$((toc_end + 1)),\$p" "$file"
    } > "$outfile"
  else
    echo "未检测到目录段，将在文件开头插入..."

    # 在文件开头插入目录段（保留一个空行分隔）
    {
      printf "%s\n\n" "$toc"
      cat "$file"
    } > "$outfile"
  fi

  # 5. 写回原文件
  mv "$outfile" "$file"

  echo "✓ 目录生成完成: $file"
}

main "$@"
