// 临时探针：测量解析/持有 manifests 的真实 RSS 成本
use agentos_core::traits::PluginManifest;
use std::collections::HashMap;

// Windows 进程工作集（GetProcessMemoryInfo，kernel32 导出，免额外依赖）
#[repr(C)]
struct ProcessMemoryCounters {
    cb: u32,
    page_fault_count: u32,
    peak_working_set_size: usize,
    working_set_size: usize,
    quota_peak_paged_pool_usage: usize,
    quota_paged_pool_usage: usize,
    quota_peak_nonpaged_pool_usage: usize,
    quota_nonpaged_pool_usage: usize,
    pagefile_usage: usize,
    peak_pagefile_usage: usize,
}

extern "system" {
    fn K32GetProcessMemoryInfo(
        process: *mut std::ffi::c_void,
        counters: *mut ProcessMemoryCounters,
        size: u32,
    ) -> i32;
    fn GetCurrentProcess() -> *mut std::ffi::c_void;
}

fn rss_mb() -> f64 {
    unsafe {
        let mut c = std::mem::zeroed::<ProcessMemoryCounters>();
        c.cb = std::mem::size_of::<ProcessMemoryCounters>() as u32;
        if K32GetProcessMemoryInfo(GetCurrentProcess(), &mut c, c.cb) != 0 {
            c.working_set_size as f64 / (1024.0 * 1024.0)
        } else {
            0.0
        }
    }
}

fn main() {
    let dir = std::path::PathBuf::from(
        std::env::args().nth(1).unwrap_or_else(|| "src/bin".into()),
    );
    let mut walk = Vec::new();
    fn collect(dir: &std::path::Path, out: &mut Vec<std::path::PathBuf>) {
        const SKIP: &[&str] = &["node_modules", ".venv", "__pycache__", "dsh_plugins", "runtime", ".venv-hindsight", "target"];
        let Ok(entries) = std::fs::read_dir(dir) else { return };
        for entry in entries.flatten() {
            let path = entry.path();
            if path.is_dir() {
                if !SKIP.contains(&path.file_name().and_then(|n| n.to_str()).unwrap_or("")) {
                    collect(&path, out);
                }
            } else if path.file_name().and_then(|n| n.to_str()) == Some("plugin.json") {
                out.push(path);
            }
        }
    }
    collect(&dir, &mut walk);
    println!("manifests: {}", walk.len());
    println!("stage0 baseline rss={:.1}MB", rss_mb());
    std::thread::sleep(std::time::Duration::from_secs(5));

    let mut manifests: Vec<PluginManifest> = Vec::new();
    for p in &walk {
        let text = std::fs::read_to_string(p).unwrap();
        if let Ok(m) = serde_json::from_str::<PluginManifest>(&text) {
            manifests.push(m);
        }
    }
    println!("stage1 parsed {} manifests rss={:.1}MB", manifests.len(), rss_mb());
    std::thread::sleep(std::time::Duration::from_secs(5));

    // 模拟 loader cache（clone）
    let mut cache: HashMap<String, (PluginManifest, std::path::PathBuf)> = HashMap::new();
    for p in &walk {
        let text = std::fs::read_to_string(p).unwrap();
        if let Ok(m) = serde_json::from_str::<PluginManifest>(&text) {
            cache.insert(m.id.clone(), (m, p.clone()));
        }
    }
    println!("stage2 loader cache rss={:.1}MB", rss_mb());
    std::thread::sleep(std::time::Duration::from_secs(5));

    // 模拟 AppState config（to_value System/Pipeline）
    let agents: Vec<serde_json::Value> = manifests.iter()
        .filter(|m| m.plugin_type == agentos_core::traits::PluginType::System)
        .map(|m| serde_json::to_value(m).unwrap_or_default())
        .collect();
    let pipelines: Vec<serde_json::Value> = manifests.iter()
        .filter(|m| m.plugin_type == agentos_core::traits::PluginType::Pipeline)
        .map(|m| serde_json::to_value(m).unwrap_or_default())
        .collect();
    let config = serde_json::json!({"agents": agents, "pipelines": pipelines, "tools": [], "routes": {}});
    println!("stage3 AppState config rss={:.1}MB", rss_mb());
    std::thread::sleep(std::time::Duration::from_secs(5));

    // 模拟 manifests_shared clone
    let shared = manifests.clone();
    println!("stage4 shared clone rss={:.1}MB", rss_mb());
    std::thread::sleep(std::time::Duration::from_secs(5));

    // 模拟 enabled_manifests clone
    let enabled: Vec<PluginManifest> = manifests.iter().cloned().collect();
    println!("stage5 enabled clone rss={:.1}MB", rss_mb());
    std::thread::sleep(std::time::Duration::from_secs(5));

    // 模拟 state.clone() 3 次（build_router 路径）
    let mut state_copies = Vec::new();
    for _ in 0..3 {
        state_copies.push(config.clone());
    }
    println!("stage6 3x config clones rss={:.1}MB", rss_mb());
    std::thread::sleep(std::time::Duration::from_secs(30));
    println!("done rss={:.1}MB", rss_mb());
}
