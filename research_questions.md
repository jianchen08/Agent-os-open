# 通用下载工具最佳实现方案调研 — 研究问题清单

## 调研目标
为创建通用文件下载工具选择最佳实现方案。

## 调研子方向与具体问题

### 子方向 1：Python 生态中成熟的下载库对比 [must]
- Q1.1: aria2（RPC/CLI）的功能特性、优缺点、活跃度？
- Q1.2: pySmartDL 的功能特性、优缺点、活跃度？是否支持分段下载和断点续传？
- Q1.3: aria2p（aria2 Python wrapper）的功能特性、优缺点？
- Q1.4: httpx 的下载能力（流式、分段、重试）？
- Q1.5: aiohttp 的下载能力？
- Q1.6: requests/aiohttp/httpx 的文件下载场景定位？
- Q1.7: 其他值得关注的库（如 wget.py、urlretrieve、urllib3）？

### 子方向 2：aria2 RPC 模式 vs CLI 模式 [must]
- Q2.1: aria2 RPC 模式的工作原理、接口、认证机制？
- Q2.2: aria2 CLI 模式的调用方式、参数控制？
- Q2.3: Python 如何通过 jsonrpc 调用 aria2（推荐库）？
- Q2.4: RPC vs CLI 的优劣对比？

### 子方向 3：纯 Python 方案的分段下载和断点续传 [must]
- Q3.1: 分段下载（Range/Content-Range）HTTP 协议机制？
- Q3.2: Python 实现 Range 请求的标准方法（urllib3、httpx）？
- Q3.3: 断点续传实现（持久化状态、合并分片）？
- Q3.4: 线程池/异步并发下载分片的实现模式？
- Q3.5: 多分片文件的完整性校验（E-tag、Content-Length）？

### 子方向 4：安全限制实现 [must]
- Q4.1: 文件大小上限（Content-Length 校验、流式累积）？
- Q4.2: 域名白名单/黑名单最佳实践？
- Q4.3: 防止路径穿越（file name 清洗、绝对路径限制）？
- Q4.4: 防止 SSRF（内网地址过滤、URL 校验）？
- Q4.5: HTTPS/TLS 校验、证书固定？
- Q4.6: 重定向处理的安全风险？

### 子方向 5：测试方案 [should]
- Q5.1: 公开的测试文件 URL 列表（支持 Range 的 HTTP 服务）？
- Q5.2: 如何测试断点续传（中断-恢复场景）？
- Q5.3: 如何测试并发分段下载的正确性？
- Q5.4: 本地起 HTTP 服务器做集成测试的方法？
- Q5.5: 离线/降级测试策略？

## 关注点专题 [must]
- C1: 通用性 — 库 API 易用性、跨平台支持、Python 版本兼容
- C2: 功能完整性 — 分段、断点续传、重试、进度回调、并发
- C3: 性能 — 速度、内存占用、连接复用
- C4: 健壮性 — 错误处理、网络异常、超时、重连
- C5: 安全性 — 协议、路径、域名、SSRF
- C6: 可测试性 — 依赖、mock、集成测试

## 优先级
- must: 子方向 1-4、专题 C1-C5
- should: 子方向 5
- nice_to_have: 极端场景（IPv6、代理、SOCKS）

## 调研严格度
standard（适中，每个问题至少 2 个来源）

## 输出要求
1. 调研报告输出到 docs/调研通用下载工具最佳实现方案_research_report.md
2. 中间笔记可放 docs/working/

## 待办事项
- [ ] 1. 拆解调研子方向并写入 research_questions.md（已完成）
- [ ] 2. 调研 aria2（功能、RPC/CLI、Python 集成）
- [ ] 3. 调研 pySmartDL（功能、API、活跃度）
- [ ] 4. 调研 aria2p（功能、API、活跃度）
- [ ] 5. 调研 httpx/aiohttp（下载能力、Range、流式）
- [ ] 6. 调研纯 Python 分段下载和断点续传实现
- [ ] 7. 调研安全最佳实践（路径穿越、SSRF、大小限制）
- [ ] 8. 调研测试方案和公开测试 URL
- [ ] 9. 编写初稿（基本结构+核心发现）
- [ ] 10. 补充完善（矛盾处理、风险、建议）
- [ ] 11. 自评审计并提交
