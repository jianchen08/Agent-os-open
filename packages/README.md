# 离线包目录

镜像构建时**优先**从此目录读取依赖，目录为空时自动回退到远程多镜像链。

| 目录 | 内容 | 用于 |
|------|------|------|
| `wheels/` | `*.whl` Python 轮子 | `frontend/Dockerfile`（uvicorn/fastapi/httpx/websockets）+ 根 `Dockerfile`（requirements.txt）|
| `npm-tarballs/` | `*.tgz` npm 包 tarball | `frontend/Dockerfile` 的 `npm ci`/`npm install` |

## 如何预下载

### Python wheels
在能联网的机器上一次性下载全部依赖：

```bash
# 后端依赖（根 requirements.txt）
pip download -r requirements.txt -d packages/wheels/

# 前端运行时依赖（frontend/Dockerfile 第 2 阶段用到的 4 个包）
pip download \
    "uvicorn[standard]==0.38.0" "fastapi==0.121.3" \
    "httpx==0.28.1" "websockets==16.0" \
    -d packages/wheels/
```

### npm tarballs
npm 的离线安装较复杂（依赖完整传递闭包）。推荐用 `npm pack` 批量打包 `package-lock.json` 中的依赖，或直接保留完整 `node_modules` 后改用 `npm ci --prefer-offline`：

```bash
cd frontend
# 方式 1：打包锁文件中的所有包到 packages/npm-tarballs/
node -e "const l=require('./package-lock.json');Object.entries(l.packages||{}).filter(([k])=>k).forEach(([k,v])=>{if(v.resolved&&v.integrity){console.log(k)}})" \
  | xargs -I{} npm pack {}

# 方式 2（更简单）：仅放最关键包，其余走镜像回退
# 留空即可，构建会自动走 npmmirror → 官方 回退
```

## 行为说明

- **目录非空**：Dockerfile 使用 `--no-index`/`--offline` 完全离线安装，**不联网**
- **目录为空**：Dockerfile 按以下镜像链回退：
  - pip：阿里云 → 清华 tuna → 官方 PyPI
  - npm：淘宝 npmmirror → 官方 npmjs.org
- 二进制包（`.whl`/`.tgz`）**不进 git**（见根 `.gitignore`），仅保留 `.gitkeep` 占位
