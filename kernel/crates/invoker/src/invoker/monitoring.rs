//! 监控 M3：宿主进程态快照（api crate 进程态轮询任务的取数面）。
//!
//! 自 invoker.rs 按关注点纯搬运拆出（2026-10-12 文件长度机械闸首个应用案例，
//! ADR docs/decisions/2026-10-12-file-length-mechanical-gate.md）。

use std::time::SystemTime;

use agentos_core::traits::{HostBox, HostBoxKind, HostBoxMember, PendingSpawnEntry};

use super::host_key::{light_host_max_members, parse_group_slot, solo_host_key};

use super::*;

// ═══════════════════════════════════════════════════════════════════════════
// 监控 M3：宿主进程态快照（api crate 进程态轮询任务的取数面）
// ═══════════════════════════════════════════════════════════════════════════

/// 宿主进程态快照行（一次采集、一个宿主进程；合宿宿主的成员共享该进程）。
#[derive(Debug, Clone)]
pub struct HostProcSnapshot {
    pub host_key: String,
    /// stdio 宿主的 OS 进程号；HTTP transport / 未连接 = None
    pub pid: Option<u32>,
    /// 未判死即 true（判死门控同 is_dead_sidecar；HTTP transport 恒 true）
    pub alive: bool,
    /// 宿主进程运行秒数（spawn 时刻起算；无记账时 None）
    pub uptime_secs: Option<u64>,
    /// 本宿主承载的成员插件 id（合宿宿主 = 多个；轻量组按分配表，独占按宿主键前缀）
    pub plugin_ids: Vec<String>,
}

impl PluginInvokerImpl {
    /// 枚举全部在册宿主的进程态快照（只读：不判死驱逐、不 touch、不 spawn）。
    ///
    /// 锁序纪律：mcp_clients / host_spawned_at 是 parking_lot 锁，先克隆收窄
    /// 立即放锁，再对每个 client 做 tokio 锁内的 async 判死/pid 读取——保证
    /// 返回 future Send（调用方在 tokio::spawn 任务里 await）。
    pub async fn host_proc_snapshots(&self) -> Vec<HostProcSnapshot> {
        let pairs: Vec<(String, SharedMcpClient)> = {
            let clients = self.mcp_clients.read();
            clients
                .iter()
                .map(|(k, v)| (k.clone(), Arc::clone(v)))
                .collect()
        };
        let uptimes: HashMap<String, Option<u64>> = {
            let spawned = self.host_spawned_at.read();
            spawned
                .iter()
                .map(|(k, t)| (k.clone(), Some(t.elapsed().as_secs())))
                .collect()
        };
        let members: HashMap<String, Vec<String>> = pairs
            .iter()
            .map(|(k, _)| (k.clone(), self.host_members(k)))
            .collect();

        let mut out = Vec::with_capacity(pairs.len());
        for (host_key, client) in pairs {
            let guard = client.read().await;
            let alive = !Self::is_dead_sidecar(&guard).await;
            let pid = guard.pid().await;
            drop(guard);
            out.push(HostProcSnapshot {
                uptime_secs: uptimes.get(&host_key).copied().flatten(),
                plugin_ids: members.get(&host_key).cloned().unwrap_or_default(),
                host_key,
                pid,
                alive,
            });
        }
        out
    }

    /// 组装宿主盒子快照（`GET /api/v1/plugins/hosts` 取数面，只读：不 spawn、
    /// 不驱逐、不 touch、不判死驱逐）。
    ///
    /// 锁序纪律与 [`Self::host_proc_snapshots`] 同构：parking_lot 锁先克隆
    /// 收窄立即放锁，再对每个 client 做 tokio 锁内的 async 判死/pid 读取。
    /// pid/alive/uptime 复用 host_proc_snapshots 采集面；commit_mb 归 api 侧富化
    /// （私有提交采集面在 api crate，此处恒 None）。
    ///
    /// pending_spawn 口径：invoker 记账面可见的插件 id 全集（装箱分配表 ∪
    /// 成员调用时钟 ∪ 独占宿主键 ∪ 启动窗口）对「有分配表条目 ∨ 有宿主缓存
    /// 条目（在场即算，判死属 async 观测面不入差集）」的差集。全量启用清单
    /// （L1 enabled × manifests）归 AppState 持有，不在 invoker 视野——从未
    /// 触达 invoker 的插件不在此列。
    pub(super) async fn host_boxes_snapshot_impl(&self) -> HostBoxesSnapshot {
        let pairs: Vec<(String, SharedMcpClient)> = {
            let clients = self.mcp_clients.read();
            clients
                .iter()
                .map(|(k, v)| (k.clone(), Arc::clone(v)))
                .collect()
        };
        let uptimes: HashMap<String, Option<u64>> = {
            let spawned = self.host_spawned_at.read();
            spawned
                .iter()
                .map(|(k, t)| (k.clone(), Some(t.elapsed().as_secs())))
                .collect()
        };
        let spawned_sets: HashMap<String, Vec<String>> = self.spawned_members.read().clone();
        let last_used: HashMap<String, Instant> = self.member_last_used.read().clone();
        let starting: std::collections::HashSet<String> = self.starting_plugins.read().clone();
        let assignments: HashMap<String, String> = self.light_packing.read().assignments.clone();
        let host_keys: std::collections::HashSet<String> =
            pairs.iter().map(|(k, _)| k.clone()).collect();

        // member_last_used 记 Instant（单调钟），契约要求 epoch 秒——以当前
        // wall-clock 为基准回退 elapsed 换算（显示面精度足够，不作计时判据）。
        let now_epoch = SystemTime::now()
            .duration_since(SystemTime::UNIX_EPOCH)
            .map(|d| d.as_secs_f64())
            .unwrap_or(0.0);

        let mut hosts = Vec::with_capacity(pairs.len());
        for (host_key, client) in pairs {
            let (pid, alive) = {
                let guard = client.read().await;
                (guard.pid().await, !Self::is_dead_sidecar(&guard).await)
            };
            let kind = if parse_group_slot(&host_key).is_some() {
                HostBoxKind::Group
            } else {
                HostBoxKind::Solo
            };
            let member_ids = self.host_members(&host_key);
            let spawned_set = spawned_sets.get(&host_key);
            // spawned_members 只记合宿 spawn 面：独占宿主缺条目 = 进程服务面
            // 即键内嵌插件自身；组宿主缺快照属异常态（spawn 不变量：快照先于
            // 入缓存），如实出空集（成员全部渲染为待重加入，fail-closed 显示）。
            let spawned_members_out = match (kind.clone(), spawned_set) {
                (HostBoxKind::Solo, _) => member_ids.clone(),
                (HostBoxKind::Group, Some(set)) => {
                    let mut v = set.clone();
                    v.sort();
                    v
                }
                (HostBoxKind::Group, None) => Vec::new(),
            };
            let members = member_ids
                .iter()
                .map(|plugin_id| HostBoxMember {
                    plugin_id: plugin_id.clone(),
                    pending_rejoin: match (&kind, spawned_set) {
                        // 独占成员无重加入概念（无组 respawn 面）
                        (HostBoxKind::Solo, _) => false,
                        // 组成员在分配表但不在 spawn 快照 = 待重加入；快照缺失
                        // 按不在处理（与漂移检测 fail-closed 同口径）
                        (HostBoxKind::Group, set) => set.is_none_or(|s| !s.contains(plugin_id)),
                    },
                })
                .collect();
            let starting_members: Vec<String> = member_ids
                .iter()
                .filter(|id| starting.contains(*id))
                .cloned()
                .collect();
            let mut member_in_flight: HashMap<String, u64> = HashMap::new();
            let mut last_call_at: Option<f64> = None;
            for plugin_id in &member_ids {
                let n = self.member_inflight(plugin_id) as u64;
                if n > 0 {
                    member_in_flight.insert(plugin_id.clone(), n);
                }
                if let Some(t) = last_used.get(plugin_id) {
                    let epoch = now_epoch - t.elapsed().as_secs_f64();
                    if last_call_at.is_none_or(|c| epoch > c) {
                        last_call_at = Some(epoch);
                    }
                }
            }
            hosts.push(HostBox {
                uptime_secs: uptimes.get(&host_key).copied().flatten(),
                in_flight: self.host_inflight(&host_key) as u64,
                spawned_members: spawned_members_out,
                last_call_at,
                starting: !starting_members.is_empty(),
                starting_members,
                members,
                slot_used: member_ids.len() as u64,
                slot_cap: match &kind {
                    HostBoxKind::Group => light_host_max_members() as u64,
                    HostBoxKind::Solo => 1,
                },
                member_in_flight,
                host_key,
                kind,
                pid,
                alive,
                commit_mb: None,
            });
        }
        hosts.sort_by(|a, b| a.host_key.cmp(&b.host_key));

        let mut universe: std::collections::BTreeSet<String> = std::collections::BTreeSet::new();
        universe.extend(assignments.keys().cloned());
        universe.extend(last_used.keys().cloned());
        universe.extend(starting.iter().cloned());
        universe.extend(
            host_keys
                .iter()
                .filter_map(|k| k.strip_prefix("plugin:"))
                .map(str::to_string),
        );
        let pending_spawn = universe
            .into_iter()
            .filter(|id| !assignments.contains_key(id) && !host_keys.contains(&solo_host_key(id)))
            .map(|id| PendingSpawnEntry {
                enabled: self.loader.get_manifest(&id).is_some(),
                last_call_at: last_used
                    .get(&id)
                    .map(|t| now_epoch - t.elapsed().as_secs_f64()),
                plugin_id: id,
            })
            .collect();
        HostBoxesSnapshot {
            hosts,
            pending_spawn,
        }
    }
}
