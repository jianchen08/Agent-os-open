# 安全策略

> 本文档说明如何报告 **灵汐 AgentOS** 的安全漏洞，以及我们的处理流程。

---

## 📣 支持的版本

下表列出当前获得安全更新的版本：

| 版本 | 是否支持 |
|------|----------|
| 0.1.x（最新） | ✅ 是 |
| < 0.1.0 | ❌ 否 |

我们仅为最新主版本提供安全修复。发现旧版本漏洞请升级后重测。

---

## 🚨 报告漏洞

**请勿**通过公开 GitHub Issues 报告安全漏洞。

### 推荐渠道（按优先级）

1. **GitHub Security Advisories**（推荐）
   - 访问 https://github.com/AI-agent-system/Agent-os/security/advisories/new
   - 填写漏洞详情（含复现步骤、影响范围、建议修复方案）

2. **邮件**
   - 发送至：`chenjian1306792950@foxmail.com`
   - 邮件主题前缀：`[Security]`
   - 邮件正文使用英文，便于国际安全研究者协作

### 报告应包含

- 漏洞类型（如 SQL 注入、XSS、远程代码执行、SSRF 等）
- 受影响的版本
- 受影响的文件 / 模块路径
- 复现步骤（PoC 代码 / 截图 / 录屏）
- 潜在影响范围
- 是否已公开披露

---

## ⏱️ 处理流程与 SLA

| 阶段 | 时间 | 我们的动作 |
|------|------|-----------|
| 收到报告 | T+24h | 确认收到，分配跟踪编号 |
| 初步评估 | T+3 天 | 评估严重等级、影响范围、复现可行性 |
| 修复开发 | T+7~30 天 | 取决于严重等级（详见下表） |
| 致谢 / 公开发布 | 修复后 | CVE 申请（如适用）+ Release Notes + 致谢 |

### 严重等级与修复目标

| 等级 | 描述 | 修复目标 |
|------|------|----------|
| 🔴 严重 | 远程代码执行、权限提升、数据泄露 | 7 天内 |
| 🟡 高危 | 认证绕过、敏感信息泄露 | 14 天内 |
| 🟢 中危 | 有限的权限提升、DoS | 30 天内 |
| ⚪ 低危 | 信息泄露、边界场景异常 | 下一版本 |

---

## 🙏 致谢

我们感谢每一位负责任地披露漏洞的研究者。经您同意后，您的名字将出现在：

- 发布公告的致谢名单
- 本项目 `AUTHORS.md` 的"安全贡献者"小节

---

## 🔐 安全最佳实践（给使用者）

部署灵汐 AgentOS 时，请遵循以下安全建议：

1. **密钥管理**
   - 所有 API Key 通过 `.env` 文件注入，不要硬编码
   - 生产环境必须轮换默认 `APP_JWT_SECRET_KEY` 和 `DEFAULT_ADMIN_PASSWORD`
   - `.env` 文件权限设为 `chmod 600`

2. **网络安全**
   - 生产部署务必启用 HTTPS（反向代理 Nginx / Caddy）
   - 不要将后端端口 `8988` 直接暴露到公网
   - Redis 端口 `6480` 应仅监听内网或 Unix Socket

3. **认证与授权**
   - 启用 RBAC（参考 `src/auth/rbac.py`）
   - 定期审计 `audit_logs`（参考 `src/monitoring/`）
   - 关闭未使用的 IM 通道适配器

4. **依赖安全**
   - 定期运行 `pip-audit` 和 `npm audit`
   - 关注 GitHub Security Advisories 的告警
   - 启用 Dependabot 自动升级 PR

5. **代码执行隔离**
   - 容器任务默认在 Docker 隔离环境执行（`src/isolation/providers/docker_provider.py`）
   - 不要禁用 `isolation_policy.yaml` 中的安全规则

---

## 📚 相关资源

- [OWASP Top 10](https://owasp.org/www-project-top-ten/)
- [CWE - Common Weakness Enumeration](https://cwe.mitre.org/)
- [GitHub Security Lab](https://securitylab.github.com/)

---

> 🛡️ **安全是社区共同的责任** —— 感谢你帮助灵汐 AgentOS 变得更安全。