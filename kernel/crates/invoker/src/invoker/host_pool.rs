// @feature: FP-0.2.内核内存治理 G2b 宿主保活池+max_requests 轮换 | @ci: rust-test
//! 宿主保活池与请求数轮换（ADR 2026-10-19-memory-governor-and-process-pooling
//! §4 D1；业界对照 B6 keep-warm worker / B8 max_requests）。
//!
//! 语义：宿主空闲由「杀进程」改「保活池」——空闲回收扫描对「最近使用且池内
//! 配额内」的宿主免杀（成员软卸载与整组 kill 都跳过，进程与服务面保持完整，
//! 下次调用 fast path 直接复用），以常驻 N 个空闲宿主进程换风暴归零；这笔账
//! 经 env 显式化（默认关=旧行为；每个常驻宿主 ~200-600MB commit，ADR §6）。
//! 长跑慢性膨胀双保险：TTL 按进程年龄硬顶总寿命；max_requests 按累计请求数
//! 标记轮换候选（gunicorn max_requests 同型），超限宿主下次空闲照旧回收，
//! 由池补位或照旧 respawn。
//!
//! 三个 env（未配置=零行为变化；keepwarm 显式正值才启用，2026-09-10「显式
//! 正值才启用」裁定同构）：
//! - `AGENTOS_HOST_POOL_KEEPWARM`：保活数，**全局 LRU 配额**（全部缓存宿主
//!   键统一排名，不分组；默认 0=池关闭=旧行为）。
//! - `AGENTOS_HOST_POOL_TTL_SECS`：池内宿主最长保活时长（默认 900s）。按
//!   进程年龄（spawn 起算）判定，超时照旧回收；0/非法回落默认——硬顶不提供
//!   关闭语义（近似关闭可设极大值；`resolve_host_est_commit_mb` 同构）。
//! - `AGENTOS_HOST_POOL_MAX_REQUESTS`：宿主累计请求数轮换上限（默认 0=关闭
//!   轮换）。超限=严格大于；轮换是保活豁免的排除项，随池启用才有消费面
//!   （池关闭时空闲回收本就治理长跑，轮换无独立生效点）。
//!
//! 边界：只做「该杀不杀」式保活，不做启动期预 spawn（ADR 非目标）；池只管
//! 不杀，换码仍走 G2a 两段式（成员级热换失败照旧整组重建）；池复用走 fast
//! path 零 spawn 不过预算闸是既有语义（G1c）；保活免杀只对缓存里有健康客户
//! 端的宿主生效，半死/死进程照旧走既有清理。

use super::*;

/// 保活池候选行（宿主粒度快照，单次 GC 扫描有效）。
pub(super) struct PoolCandidate {
    pub(super) host_key: String,
    /// 宿主最近使用距今秒数（成员级 last_used 取最小 elapsed = 最近使用的
    /// 成员决定宿主热度）；无记账 = 最久未用（LRU 自然垫底）。
    pub(super) last_used_age_secs: u64,
    /// 进程年龄秒（spawn 起算）；None = 无 spawn 记账，fail-closed 不豁免
    /// （BUG-44 同哲学：进程年龄未知按最老处理）。
    pub(super) spawned_age_secs: Option<u64>,
    /// 宿主累计请求数（B8 轮换记账源，[`super::PluginInvokerImpl::
    /// record_host_request`] 记账）。
    pub(super) request_count: u64,
}

/// 解析 `AGENTOS_HOST_POOL_KEEPWARM`（纯函数，单测面）：未设/空/0/非法 → 0
/// （池关闭=旧行为）；正整数 → 保活数。
fn resolve_pool_keepwarm(raw: Option<&str>) -> u64 {
    raw.and_then(|s| s.trim().parse::<u64>().ok())
        .filter(|&v| v >= 1)
        .unwrap_or(0)
}

/// 解析 `AGENTOS_HOST_POOL_TTL_SECS`（纯函数，单测面）：未设 → 900（默认
/// 启用）；0/非法 → 900（硬顶不提供关闭语义）；正整数 → 秒。
fn resolve_pool_ttl_secs(raw: Option<&str>) -> u64 {
    raw.and_then(|s| s.trim().parse::<u64>().ok())
        .filter(|&v| v >= 1)
        .unwrap_or(900)
}

/// 解析 `AGENTOS_HOST_POOL_MAX_REQUESTS`（纯函数，单测面）：未设/空/0/非法
/// → 0（轮换关闭）；正整数 → 上限。
fn resolve_pool_max_requests(raw: Option<&str>) -> u64 {
    raw.and_then(|s| s.trim().parse::<u64>().ok())
        .filter(|&v| v >= 1)
        .unwrap_or(0)
}

/// env 现值（GC 扫描逐轮读取；纯解析见 [`resolve_pool_keepwarm`]；api crate
/// 自证行同源消费——启动期读一次与逐轮读同值，env 运行期不热变）。
pub fn pool_keepwarm_from_env() -> u64 {
    resolve_pool_keepwarm(std::env::var("AGENTOS_HOST_POOL_KEEPWARM").ok().as_deref())
}

/// env 现值（见 [`pool_keepwarm_from_env`]）。
pub fn pool_ttl_secs_from_env() -> u64 {
    resolve_pool_ttl_secs(std::env::var("AGENTOS_HOST_POOL_TTL_SECS").ok().as_deref())
}

/// env 现值（见 [`pool_keepwarm_from_env`]）。
pub fn pool_max_requests_from_env() -> u64 {
    resolve_pool_max_requests(
        std::env::var("AGENTOS_HOST_POOL_MAX_REQUESTS")
            .ok()
            .as_deref(),
    )
}

/// 保活免杀集合判定（纯函数，单测面）：TTL 未到顶 + 非轮换候选的宿主按最近
/// 使用排序（最热优先），取前 keepwarm 名。keepwarm=0 恒空集（池关闭零行为
/// 变化）；排序按 (last_used_age_secs, host_key) 全序，同热度键序定稳。
pub(super) fn keepwarm_exempt_hosts(
    keepwarm: u64,
    ttl_secs: u64,
    max_requests: u64,
    candidates: &[PoolCandidate],
) -> std::collections::HashSet<String> {
    if keepwarm == 0 {
        return std::collections::HashSet::new();
    }
    let mut eligible: Vec<&PoolCandidate> = candidates
        .iter()
        // 轮换候选不豁免（B8）：累计请求严格大于上限即标记，下次空闲照旧回收。
        .filter(|c| max_requests == 0 || c.request_count <= max_requests)
        // TTL 硬顶（严格判定：恰好到顶即回收——「最长保活时长」不含顶格值）。
        .filter(|c| c.spawned_age_secs.is_some_and(|age| age < ttl_secs))
        .collect();
    eligible.sort_by(|a, b| {
        a.last_used_age_secs
            .cmp(&b.last_used_age_secs)
            .then_with(|| a.host_key.cmp(&b.host_key))
    });
    eligible
        .into_iter()
        .take(keepwarm as usize)
        .map(|c| c.host_key.clone())
        .collect()
}

impl super::PluginInvokerImpl {
    /// 宿主累计请求数 +1（sidecar 统一调用入口每次 capability 调用记账一次；
    /// B8 轮换源数据。respawn 覆盖写归零——计数属进程不跨代，见 invoker 结构
    /// `host_request_counts` 字段注释）。
    pub(super) fn record_host_request(&self, host_key: &str) {
        *self
            .host_request_counts
            .write()
            .entry(host_key.to_string())
            .or_insert(0) += 1;
    }

    /// 单次 GC 扫描的保活免杀集合（宿主粒度）：缓存里有健康客户端的宿主全体
    /// 排定 keepwarm 配额（LRU），TTL 到顶与轮换候选不豁免。keepwarm=0 恒空
    /// 集（池关闭零开销）。健康门：缓存缺席/判死（半死、死进程）不进候选——
    /// 照旧走既有清理路径。
    ///
    /// 锁序纪律与 monitoring 同构：parking_lot 锁先快照收窄立即放锁，再对每个
    /// client 做 tokio 锁内的 async 判死（保证 future Send）。
    pub(super) async fn pool_exempt_hosts_for_pass(&self) -> std::collections::HashSet<String> {
        let keepwarm = pool_keepwarm_from_env();
        if keepwarm == 0 {
            return std::collections::HashSet::new();
        }
        let pairs: Vec<(String, SharedMcpClient)> = {
            let clients = self.mcp_clients.read();
            clients
                .iter()
                .map(|(k, v)| (k.clone(), Arc::clone(v)))
                .collect()
        };
        let last_used: HashMap<String, Instant> = self.member_last_used.read().clone();
        let request_counts: HashMap<String, u64> = self.host_request_counts.read().clone();
        let spawned: HashMap<String, Instant> = self.host_spawned_at.read().clone();
        let mut candidates = Vec::with_capacity(pairs.len());
        for (host_key, client) in pairs {
            let healthy = {
                let guard = client.read().await;
                !Self::is_dead_sidecar(&guard).await
            };
            if !healthy {
                continue;
            }
            candidates.push(PoolCandidate {
                last_used_age_secs: self.host_last_used_age_secs(&host_key, &last_used),
                spawned_age_secs: spawned.get(&host_key).map(|t| t.elapsed().as_secs()),
                request_count: request_counts.get(&host_key).copied().unwrap_or(0),
                host_key,
            });
        }
        keepwarm_exempt_hosts(
            keepwarm,
            pool_ttl_secs_from_env(),
            pool_max_requests_from_env(),
            &candidates,
        )
    }

    /// 宿主最近使用距今秒数：成员级 last_used 取最小 elapsed（最近使用的成员
    /// 决定宿主热度）。无任何成员记账 = u64::MAX（最久未用，LRU 自然垫底）。
    fn host_last_used_age_secs(&self, host_key: &str, last_used: &HashMap<String, Instant>) -> u64 {
        self.host_members(host_key)
            .iter()
            .filter_map(|m| last_used.get(m))
            .map(|t| t.elapsed().as_secs())
            .min()
            .unwrap_or(u64::MAX)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn keepwarm_env_default_off_valid_and_invalid_fallback() {
        // 默认关：未设 / 空 / 纯空白 / 0
        assert_eq!(resolve_pool_keepwarm(None), 0);
        assert_eq!(resolve_pool_keepwarm(Some("")), 0);
        assert_eq!(resolve_pool_keepwarm(Some("   ")), 0);
        assert_eq!(resolve_pool_keepwarm(Some("0")), 0);
        // 合法：显式正值才启用（容忍首尾空白）
        assert_eq!(resolve_pool_keepwarm(Some("2")), 2);
        assert_eq!(resolve_pool_keepwarm(Some(" 3 ")), 3);
        assert_eq!(resolve_pool_keepwarm(Some("1")), 1);
        // 非法 / 负数 / 小数 / 溢出 → 回落关闭（与 host_job 限值解析同构）
        assert_eq!(resolve_pool_keepwarm(Some("abc")), 0);
        assert_eq!(resolve_pool_keepwarm(Some("-1")), 0);
        assert_eq!(resolve_pool_keepwarm(Some("1.5")), 0);
        assert_eq!(resolve_pool_keepwarm(Some("99999999999999999999999")), 0);
    }

    #[test]
    fn ttl_env_default_900_and_zero_invalid_fall_back() {
        // 默认 900s
        assert_eq!(resolve_pool_ttl_secs(None), 900);
        assert_eq!(resolve_pool_ttl_secs(Some("")), 900);
        // 合法覆盖
        assert_eq!(resolve_pool_ttl_secs(Some("600")), 600);
        assert_eq!(resolve_pool_ttl_secs(Some(" 1200 ")), 1200);
        assert_eq!(resolve_pool_ttl_secs(Some("1")), 1);
        // 0 / 非法 → 回落默认：硬顶不提供关闭语义（近似关闭设极大值）
        assert_eq!(resolve_pool_ttl_secs(Some("0")), 900);
        assert_eq!(resolve_pool_ttl_secs(Some("abc")), 900);
        assert_eq!(resolve_pool_ttl_secs(Some("-5")), 900);
        assert_eq!(resolve_pool_ttl_secs(Some("1.5")), 900);
    }

    #[test]
    fn max_requests_env_default_off_valid_and_invalid_fallback() {
        // 默认关：未设 / 空 / 0
        assert_eq!(resolve_pool_max_requests(None), 0);
        assert_eq!(resolve_pool_max_requests(Some("")), 0);
        assert_eq!(resolve_pool_max_requests(Some("0")), 0);
        // 合法
        assert_eq!(resolve_pool_max_requests(Some("100")), 100);
        assert_eq!(resolve_pool_max_requests(Some(" 50 ")), 50);
        // 非法 → 回落关闭
        assert_eq!(resolve_pool_max_requests(Some("abc")), 0);
        assert_eq!(resolve_pool_max_requests(Some("-3")), 0);
        assert_eq!(resolve_pool_max_requests(Some("2.5")), 0);
    }

    fn cand(host: &str, last_used: u64, age: Option<u64>, requests: u64) -> PoolCandidate {
        PoolCandidate {
            host_key: host.to_string(),
            last_used_age_secs: last_used,
            spawned_age_secs: age,
            request_count: requests,
        }
    }

    #[test]
    fn keepwarm_exempt_hosts_lru_quota_with_ttl_and_rotation_exclusions() {
        // 六候选四类：hot/warm/cold 合规；rotated 超请求数；expired 超 TTL；
        // nobook 无 spawn 记账。keepwarm=2 → 最热两名合规者入选（rotated/
        // expired/nobook 虽更新热也被排除）。
        let candidates = vec![
            cand("hot", 100, Some(50), 0),
            cand("warm", 400, Some(50), 0),
            cand("cold", 4000, Some(50), 0),
            cand("rotated", 10, Some(50), 11),
            cand("expired", 10, Some(2000), 0),
            cand("nobook", 10, None, 0),
        ];
        let exempt = keepwarm_exempt_hosts(2, 900, 10, &candidates);
        assert_eq!(exempt.len(), 2);
        assert!(exempt.contains("hot"));
        assert!(exempt.contains("warm"));
        // 配额收紧到 1：只剩最热者；配额扩到 10：合规者全入（cold 第三）
        assert_eq!(
            keepwarm_exempt_hosts(1, 900, 10, &candidates),
            std::iter::once("hot".to_string()).collect()
        );
        let all = keepwarm_exempt_hosts(10, 900, 10, &candidates);
        assert!(all.contains("cold"));
        assert_eq!(all.len(), 3, "轮换/超TTL/无记账三类恒不豁免");
        // 池关闭：恒空集
        assert!(keepwarm_exempt_hosts(0, 900, 10, &candidates).is_empty());
    }

    #[test]
    fn keepwarm_exempt_hosts_boundaries_are_strict() {
        // 轮换边界：计数恰好等于上限 → 仍合规（超限=严格大于）
        let at_limit = vec![cand("a", 10, Some(50), 10), cand("b", 20, Some(50), 10)];
        // 计数=上限不构成轮换候选（超限=严格大于）
        assert_eq!(keepwarm_exempt_hosts(2, 900, 10, &at_limit).len(), 2);
        let over_limit = vec![cand("a", 10, Some(50), 11)];
        // 计数>上限即轮换候选，不豁免
        assert!(keepwarm_exempt_hosts(2, 900, 10, &over_limit).is_empty());
        // TTL 边界：年龄恰好等于 TTL → 回收（「最长保活时长」不含顶格值）
        let at_ttl = vec![cand("a", 10, Some(900), 0)];
        assert!(keepwarm_exempt_hosts(2, 900, 0, &at_ttl).is_empty());
        let under_ttl = vec![cand("a", 10, Some(899), 0)];
        assert_eq!(keepwarm_exempt_hosts(2, 900, 0, &under_ttl).len(), 1);
    }

    #[test]
    fn keepwarm_exempt_hosts_lru_tiebreak_by_host_key() {
        // 同热度并列：键序定稳（判定可复现，不随输入顺序漂移）
        let tied = vec![
            cand("beta", 300, Some(50), 0),
            cand("alpha", 300, Some(50), 0),
        ];
        let exempt = keepwarm_exempt_hosts(1, 900, 0, &tied);
        assert_eq!(exempt, std::iter::once("alpha".to_string()).collect());
        let tied_rev = vec![
            cand("alpha", 300, Some(50), 0),
            cand("beta", 300, Some(50), 0),
        ];
        assert_eq!(
            keepwarm_exempt_hosts(1, 900, 0, &tied_rev),
            std::iter::once("alpha".to_string()).collect()
        );
    }
}
