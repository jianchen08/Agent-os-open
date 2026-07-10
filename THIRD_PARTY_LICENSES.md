# 第三方依赖许可证清单生成说明

> 本文件说明如何生成符合 Apache 2.0 §4(d) 要求的 `THIRD_PARTY_LICENSES.txt`。

---

## 🎯 为什么需要这个文件？

Apache License 2.0 第 4(d) 条规定：

> If the Work includes a "NOTICE" text file as part of its distribution,
> then any Derivative Works that You distribute must include a readable
> copy of the attribution notices contained within such NOTICE file...

简单说：如果你分发的作品依赖了第三方库，需要把**所有第三方许可证的全文或摘要**放在一起，方便使用者查阅。

---

## 🚀 生成步骤

### 后端（Python）

```bash
# 安装工具
pip install pip-licenses

# 生成 Markdown 格式（推荐）
pip-licenses --format=markdown --output-file=THIRD_PARTY_LICENSES.txt

# 生成 plain-vertical 格式
pip-licenses --format=plain-vertical --output-file=THIRD_PARTY_LICENSES.txt

# 仅包含直接依赖（推荐，避免重复列出传递依赖）
pip-licenses --format=markdown --output-file=THIRD_PARTY_LICENSES.txt \
  --packages="$(grep -A 100 '\[project\]' pyproject.toml | grep -E '^\s*"[a-zA-Z]' | awk -F'"' '{print $2}' | tr '\n' ' ')"
```

### 前端（Node.js）

```bash
# 安装工具
npm install -g license-checker

# 生成 CSV 格式
npx license-checker --production --csv > THIRD_PARTY_LICENSES_FRONTEND.csv

# 生成 JSON 格式（更易解析）
npx license-checker --production --json > THIRD_PARTY_LICENSES_FRONTEND.json
```

### 合并输出

```bash
# 把 Python 和 Node 的依赖清单合并到一个文件
cat THIRD_PARTY_LICENSES.txt THIRD_PARTY_LICENSES_FRONTEND.csv > THIRD_PARTY_LICENSES_COMBINED.txt
```

---

## 📋 预期内容（示例）

```
# 后端依赖（Python）

| Name      | Version | License    |
|-----------|---------|------------|
| aiohttp   | 3.9.1   | Apache-2.0 |
| fastapi   | 0.110.0 | MIT        |
| pydantic  | 2.5.0   | MIT        |
| ...       | ...     | ...        |
```

---

## ⚠️ 注意事项

1. **首次发布前必须生成** —— 这是 Apache 2.0 合规要求
2. **每次发版前重新生成** —— 依赖可能变化
3. **建议加入 CI 自动检查** —— 检测许可证变更是否被记录
4. **许可证兼容性审查** —— 如果引入 GPL 等强传染性许可证，整个项目可能需要换许可证

---

## 🔗 相关资源

- [pip-licenses 文档](https://github.com/raimon-synapsis/pip-licenses)
- [license-checker 文档](https://github.com/davglass/license-checker)
- [Apache 2.0 第 4(d) 条](https://www.apache.org/licenses/LICENSE-2.0)
- [SPDX 许可证列表](https://spdx.org/licenses/)

---

_最后更新：2026-07-02_