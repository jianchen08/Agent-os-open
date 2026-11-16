//! 日志面活性探测（BUG-71 裁决产物）。
//!
//! BUG-71（2026-09-22/23）裁决：内核日志写者并未死亡——tracing-appender 0.2.5
//! 的 daily 轮转全程以 UTC 为单一事实源（`now_utc()` 初始化与逐写检查，文件名 =
//! UTC 日期，轮转边界 = UTC 午夜 = 本地 08:00），「本地午夜停笔」实为 60s 重验证
//! 周期里 43~48s 的常态静默窗 + 操作者按本地日期找 `kernel.log.<当日>` 扑空。但
//! 该误报暴露了真实的可观测性缺陷：一旦写者真的卡死/死亡（tracing-appender
//! worker 对 IO 错误全静默吞掉，upstream worker.rs `Err(_) => {}` 自带 TODO），
//! 内核既不告警也无法被外部判活——活着与死了看起来一样。
//!
//! 本模块补两面：
//! 1. [`LivenessWriter`]：包住 RollingFileAppender 进 non_blocking，写成功则打点，
//!    写失败立即向告警面显式报告（限频防刷屏），封住「写失败零输出」。
//! 2. [`spawn_silence_watchdog`]：独立 std 线程（不依赖 tokio、不依赖日志层自身），
//!    超过阈值无成功写即向告警面报警（按阈值限频重报，非一次性）；活性新鲜时
//!    每个检查周期刷新固定名标记文件 `logs/kernel.liveness`，外部观察者 stat 这
//!    一个固定路径即可判活——绕开「当前日志文件名随 UTC 日期漂移」的找文件陷阱。
//!
//! 判别面（2026-10-13 装机停摆疑案产物）：tracing-appender 0.2.5 的 non_blocking
//! 是 lossy 丢弃制——通道满（worker 停滞）时调用方不阻塞、不知情，写手死活与
//! 「没人打日志」从调用侧不可辨别。看门狗因此多看一眼 non_blocking 丢帧计数：
//! 计数前进 = 通道满/worker 停滞，与静默阈值无关地独立告警，把「通道满丢帧」
//! 从「没人打日志」里分出来。
//!
//! 告警面为注入的闭包：生产接线为 stderr。注意装机部署 stderr 本身被丢弃
//! （electron `stdio: "ignore"`、bat supervisor → NUL），故标记文件才是权威
//! 外部判活面，stderr 报警服务于有终端可见性的入口（前台 sh、开发态）。
//! 每次告警原文另行追加落盘到 [`LIVENESS_EVIDENCE_FILENAME`]——独立于 stderr
//! 与 tracing 管线的证据面，事后复盘以它为权威（标记文件冻结与「观察者视野
//! 过期」从 stat 不可辨别，证据文件的追加行没有这个歧义）。证据文件滚动轮转
//! （F3）：活跃文件超过 [`EVIDENCE_ROTATE_BYTES`] 即重命名为 `.1`/`.2`… 代文件、
//! 超出 [`EVIDENCE_ROTATE_KEEP`] 代删除最旧——长期告警风暴下证据面磁盘占用有界。
//!
//! 三类告警（丢帧 / 活性标记写失败 / 静默）限频预算分池（F3）：每类独立配额，
//! 一类风暴不挤占他类的报告机会（共享单池时，先检查的丢帧风暴会把静默告警
//! 永久饿死——两个面恰恰是停摆复盘最需要同时在案的事实）。
// @feature: FP-0.2.可观测性 日志面活性探测(BUG-71 裁决产物) | @ci: rust-test

use std::io;
use std::io::Write as _;
use std::path::Path;
use std::sync::atomic::{AtomicI64, Ordering};
use std::sync::Arc;
use std::thread;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

/// 写失败报告的限频窗口（与静默阈值解耦：写失败是细节告警，不应等到 10 分钟）。
pub const ERR_REPORT_RATE_LIMIT: Duration = Duration::from_secs(60);

/// 生产检查周期。正常日志心跳 ≤1 分钟（60s 重验证周期必产生日志行），
/// 周期取其同量级即可——判活精度由阈值+周期共同决定。
pub const PRODUCTION_CHECK_INTERVAL: Duration = Duration::from_secs(60);

/// 生产静默阈值：无成功写超过此值判「写面疑似死亡」。常态静默窗实测 ≤48s，
/// 取 10 分钟 = 一个数量级余量，避免对正常低谷误报。
pub const PRODUCTION_SILENCE_THRESHOLD: Duration = Duration::from_secs(600);

/// 活性标记文件名（相对日志目录）。
pub const LIVENESS_MARKER_FILENAME: &str = "kernel.liveness";

/// 停摆证据文件名（相对日志目录）：每次告警原文追加落盘（UTC RFC3339 前缀）。
pub const LIVENESS_EVIDENCE_FILENAME: &str = "kernel-liveness-alarm.log";

/// 证据文件轮转阈值：活跃证据文件达到此大小即滚动为代文件（F3 上限面）。
pub const EVIDENCE_ROTATE_BYTES: u64 = 1024 * 1024;

/// 证据文件保留的滚动代数：`active → .1 → … → .{EVIDENCE_ROTATE_KEEP}`，
/// 超代删除最旧（滚动覆盖）。证据面总盘面上界 ≈ (KEEP+1) × ROTATE_BYTES。
pub const EVIDENCE_ROTATE_KEEP: usize = 2;

/// 告警面：接收一条告警文本（生产 = stderr）。
pub type AlarmSink = Arc<dyn Fn(String) + Send + Sync>;

fn now_epoch_ms() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as i64)
        .unwrap_or(0)
}

/// 最近一次成功日志写的时间戳（epoch ms），跨线程共享。
#[derive(Clone, Debug)]
pub struct LogLiveness(Arc<AtomicI64>);

impl LogLiveness {
    pub fn new() -> Self {
        Self(Arc::new(AtomicI64::new(now_epoch_ms())))
    }

    pub fn record_ok_write(&self) {
        self.0.store(now_epoch_ms(), Ordering::Release);
    }

    /// 距最近一次成功写的毫秒数。
    pub fn ms_since_ok_write(&self) -> i64 {
        now_epoch_ms() - self.0.load(Ordering::Acquire)
    }
}

impl Default for LogLiveness {
    fn default() -> Self {
        Self::new()
    }
}

/// 包住内层 writer 的活性探针：成功写打点，失败写经告警面显式报告（限频）。
///
/// 实现为 `io::Write` 透传（`write_all`/`write_fmt` 走默认实现循环调用
/// [`io::Write::write`]，轮转检查在内层每次 write 时照常发生），行为与直连
/// 内层完全一致，只多了打点与失败告警两个副作用。
pub struct LivenessWriter<W> {
    inner: W,
    liveness: LogLiveness,
    alarm_sink: AlarmSink,
    err_report_rate_limit: Duration,
    last_err_report_ms: AtomicI64,
}

impl<W> LivenessWriter<W> {
    pub fn new(
        inner: W,
        liveness: LogLiveness,
        alarm_sink: AlarmSink,
        err_report_rate_limit: Duration,
    ) -> Self {
        Self {
            inner,
            liveness,
            alarm_sink,
            err_report_rate_limit,
            last_err_report_ms: AtomicI64::new(0),
        }
    }

    fn report_err(&self, err: &io::Error) {
        let now = now_epoch_ms();
        let limit_ms = self.err_report_rate_limit.as_millis() as i64;
        let last = self.last_err_report_ms.load(Ordering::Acquire);
        if now - last < limit_ms {
            return;
        }
        if self
            .last_err_report_ms
            .compare_exchange(last, now, Ordering::AcqRel, Ordering::Acquire)
            .is_ok()
        {
            (self.alarm_sink)(format!(
                "[log_liveness] 日志写入失败（写失败会被上游丢弃，此为唯一显式报告）: {err}"
            ));
        }
    }
}

impl<W: io::Write> io::Write for LivenessWriter<W> {
    fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
        match self.inner.write(buf) {
            Ok(n) => {
                self.liveness.record_ok_write();
                Ok(n)
            }
            Err(e) => {
                self.report_err(&e);
                Err(e)
            }
        }
    }

    fn flush(&mut self) -> io::Result<()> {
        match self.inner.flush() {
            Ok(()) => {
                self.liveness.record_ok_write();
                Ok(())
            }
            Err(e) => {
                self.report_err(&e);
                Err(e)
            }
        }
    }
}

/// 静默看门狗配置。
#[derive(Clone, Copy, Debug)]
pub struct WatchdogConfig {
    /// 无成功写超过此时长即报警。
    pub silence_threshold: Duration,
    /// 检查周期。
    pub check_interval: Duration,
}

impl WatchdogConfig {
    pub fn production() -> Self {
        Self {
            silence_threshold: PRODUCTION_SILENCE_THRESHOLD,
            check_interval: PRODUCTION_CHECK_INTERVAL,
        }
    }
}

/// 单类告警的限频池（F3 分池）：每类告警独立持有配额，到期放行一次。
#[derive(Debug)]
struct AlarmRatePool {
    min_interval_ms: i64,
    last_ms: i64,
}

impl AlarmRatePool {
    fn new(min_interval_ms: i64) -> Self {
        Self {
            min_interval_ms: min_interval_ms.max(1),
            last_ms: 0,
        }
    }

    /// 到期则占用本次配额并返回 true；窗口内返回 false（不消耗）。
    fn acquire(&mut self, now_ms: i64) -> bool {
        if now_ms - self.last_ms < self.min_interval_ms {
            return false;
        }
        self.last_ms = now_ms;
        true
    }
}

/// 启动静默看门狗线程（进程存续期常驻；JoinHandle 即弃，线程自行 detach）。
///
/// 每个检查周期：
/// - 丢帧计数前进（通道满/worker 停滞）→ 告警 + 证据落盘；
/// - 活性新鲜（距上次成功写 ≤ 阈值）→ 刷新标记文件（进程号 + 打点时刻）；
/// - 活性过期 → 按阈值限频向告警面报警（持续静默则周期性重报，非一次性）。
///
/// 三类告警各自独立限频池（F3 分池）：丢帧风暴 / 标记写失败 / 静默互不挤占。
///
/// 标记文件写失败同样报警——它是外部判活的唯一权威面。
///
/// `drops`：non_blocking 丢帧计数器（生产 = fmt file layer writer 的
/// `error_counter()`；测试无 non_blocking 面时可传 `None`）。lossy 丢弃制下
/// 这是「通道满/写手停滞」的唯一显式信号。
pub fn spawn_silence_watchdog(
    liveness: LogLiveness,
    log_dir: &Path,
    config: WatchdogConfig,
    alarm_sink: AlarmSink,
    drops: Option<tracing_appender::non_blocking::ErrorCounter>,
) {
    let marker_path = log_dir.join(LIVENESS_MARKER_FILENAME);
    let evidence_path = log_dir.join(LIVENESS_EVIDENCE_FILENAME);
    let threshold_ms = config.silence_threshold.as_millis() as i64;
    thread::spawn(move || {
        let mut last_dropped = 0usize;
        // F3 分池：每类告警独立限频预算，窗口与静默阈值同长。
        let mut drops_pool = AlarmRatePool::new(threshold_ms);
        let mut marker_pool = AlarmRatePool::new(threshold_ms);
        let mut silence_pool = AlarmRatePool::new(threshold_ms);
        loop {
            thread::sleep(config.check_interval);
            let now = now_epoch_ms();
            // 丢帧面监测：计数前进 = 通道满/worker 停滞（lossy 丢弃，调用方
            // 不阻塞也不知情）。与静默阈值解耦、按阈值限频——停滞停止后自动
            // 归零，无刷屏面；报累计值（计数器本身饱和累加，跨周期可对账）。
            if let Some(counter) = drops.as_ref() {
                let dropped = counter.dropped_lines();
                if dropped > last_dropped {
                    last_dropped = dropped;
                    if drops_pool.acquire(now) {
                        let msg = format!(
                            "[log_liveness] non_blocking 丢帧计数已达 {dropped} 行\
                             （通道满/worker 停滞，调用方不阻塞）"
                        );
                        (alarm_sink)(msg.clone());
                        record_evidence(&evidence_path, &msg, &alarm_sink);
                    }
                }
            }
            let since_ok = liveness.ms_since_ok_write();
            if since_ok <= threshold_ms {
                if let Err(e) = write_liveness_marker(&marker_path, now_epoch_ms() - since_ok) {
                    if marker_pool.acquire(now_epoch_ms()) {
                        let msg =
                            format!("[log_liveness] 活性标记文件写入失败（外部判活面失效）: {e}");
                        (alarm_sink)(msg.clone());
                        record_evidence(&evidence_path, &msg, &alarm_sink);
                    }
                }
            } else if silence_pool.acquire(now_epoch_ms()) {
                let msg = format!(
                    "[log_liveness] 内核日志已静默 {}s（阈值 {}s），写面疑似死亡；\
                     外部判活标记 {} 已停止刷新",
                    since_ok / 1000,
                    threshold_ms / 1000,
                    marker_path.display(),
                );
                (alarm_sink)(msg.clone());
                record_evidence(&evidence_path, &msg, &alarm_sink);
            }
        }
    });
}

/// 告警原文追加落盘（UTC RFC3339 前缀，单行自包含）。写失败经告警面显式报告
/// （观测面不静默吞错）；调用侧已限频，无刷盘风暴面。活跃文件超过
/// [`EVIDENCE_ROTATE_BYTES`] 先滚动（超 [`EVIDENCE_ROTATE_KEEP`] 代覆盖最旧），
/// 证据面磁盘占用有界。
fn record_evidence(path: &Path, msg: &str, alarm_sink: &AlarmSink) {
    record_evidence_with_limits(
        path,
        msg,
        alarm_sink,
        EVIDENCE_ROTATE_BYTES,
        EVIDENCE_ROTATE_KEEP,
    );
}

/// [`record_evidence`] 的限额参数化形态（生产常量注入入口；测试以小限额驱动
/// 多轮滚动，不必写满 1 MiB）。
fn record_evidence_with_limits(
    path: &Path,
    msg: &str,
    alarm_sink: &AlarmSink,
    rotate_bytes: u64,
    keep: usize,
) {
    let line = format!("{} {msg}\n", chrono::Utc::now().to_rfc3339());
    if let Err(e) = rotate_evidence_if_oversized(path, rotate_bytes, keep) {
        (alarm_sink.clone())(format!("[log_liveness] 停摆证据轮转失败: {e}"));
    }
    let result = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)
        .and_then(|mut file| file.write_all(line.as_bytes()));
    if let Err(e) = result {
        (alarm_sink.clone())(format!("[log_liveness] 停摆证据写入失败: {e}"));
    }
}

/// 活跃证据文件达到 `rotate_bytes` 即滚动：最旧一代删除（滚动覆盖），其余代
/// 依次后移一位，活跃文件补位 `.1`。文件不存在 = 首次落盘，直接放行。
fn rotate_evidence_if_oversized(path: &Path, rotate_bytes: u64, keep: usize) -> io::Result<()> {
    let size = match std::fs::metadata(path) {
        Ok(m) => m.len(),
        Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(()),
        Err(e) => return Err(e),
    };
    if size < rotate_bytes {
        return Ok(());
    }
    if keep == 0 {
        return std::fs::remove_file(path);
    }
    let oldest = rotated_path(path, keep);
    if let Err(e) = std::fs::remove_file(&oldest) {
        if e.kind() != io::ErrorKind::NotFound {
            return Err(e);
        }
    }
    for gen in (1..keep).rev() {
        let from = rotated_path(path, gen);
        if from.exists() {
            std::fs::rename(&from, rotated_path(path, gen + 1))?;
        }
    }
    std::fs::rename(path, rotated_path(path, 1))
}

/// 第 `gen` 代轮转文件路径：`kernel-liveness-alarm.log` → `….log.1`。
fn rotated_path(path: &Path, gen: usize) -> std::path::PathBuf {
    let mut os = path.as_os_str().to_os_string();
    os.push(format!(".{gen}"));
    std::path::PathBuf::from(os)
}

fn write_liveness_marker(path: &Path, ok_write_epoch_ms: i64) -> io::Result<()> {
    let content = format!("{} {}\n", std::process::id(), ok_write_epoch_ms);
    std::fs::write(path, content)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::VecDeque;
    use std::sync::Mutex;

    /// 收集告警文本的测试告警面（真实回调，非 mock 依赖）。
    fn collecting_sink() -> (AlarmSink, Arc<Mutex<VecDeque<String>>>) {
        let log: Arc<Mutex<VecDeque<String>>> = Arc::new(Mutex::new(VecDeque::new()));
        let sink_log = log.clone();
        let sink: AlarmSink = Arc::new(move |msg: String| {
            sink_log.lock().unwrap().push_back(msg);
        });
        (sink, log)
    }

    /// 把活性打点回拨到指定毫秒前（构造「静默中」与「活跃」两组区分输入）。
    fn backdate(liveness: &LogLiveness, ms: i64) {
        liveness.0.store(now_epoch_ms() - ms, Ordering::Release);
    }

    fn temp_log_dir() -> (tempfile::TempDir, std::path::PathBuf) {
        let dir = tempfile::tempdir().expect("tempdir");
        let path = dir.path().join("logs");
        std::fs::create_dir_all(&path).expect("create logs dir");
        (dir, path)
    }

    /// 轮询等证据文件落盘（3s 上限）。告警行进 sink 与证据文件写完成之间
    /// 无先后保证——满套件负载下读完即断言是竞态（装机打包链实测抖红）。
    fn wait_evidence_file(log_dir: &std::path::Path) -> String {
        let evidence = log_dir.join(LIVENESS_EVIDENCE_FILENAME);
        let deadline = std::time::Instant::now() + Duration::from_secs(3);
        loop {
            if let Ok(content) = std::fs::read_to_string(&evidence) {
                return content;
            }
            assert!(
                std::time::Instant::now() < deadline,
                "3s 内证据文件未落盘: {}",
                evidence.display()
            );
            thread::sleep(Duration::from_millis(10));
        }
    }

    // --- 看门狗：静默报警 / 活跃不报 / 限频重报 ---

    #[test]
    fn evidence_wait_polls_until_file_appears() {
        // 迟到落盘：50ms 后另一线程写证据文件 → 首轮 miss 走 sleep 重试分支
        let (_dir, log_dir) = temp_log_dir();
        let writer_dir = log_dir.clone();
        thread::spawn(move || {
            thread::sleep(Duration::from_millis(50));
            std::fs::write(writer_dir.join(LIVENESS_EVIDENCE_FILENAME), "丢帧证据").unwrap();
        });
        let content = wait_evidence_file(&log_dir);
        assert!(content.contains("丢帧"), "重试后应读到迟到内容: {content}");
    }

    #[test]
    #[should_panic(expected = "3s 内证据文件未落盘")]
    fn evidence_wait_times_out_when_file_never_appears() {
        // 文件永不出现 → 3s 到点 assert 落 panic（超时分支契约）
        let (_dir, log_dir) = temp_log_dir();
        let _ = wait_evidence_file(&log_dir);
    }

    #[test]
    fn watchdog_alarms_after_silence_and_freezes_marker() {
        let (_dir, log_dir) = temp_log_dir();
        let liveness = LogLiveness::new();
        backdate(&liveness, 5_000);
        let (sink, log) = collecting_sink();

        spawn_silence_watchdog(
            liveness.clone(),
            &log_dir,
            WatchdogConfig {
                silence_threshold: Duration::from_millis(200),
                check_interval: Duration::from_millis(20),
            },
            sink,
            None,
        );

        // 静默持续 → 必须在宽松时限内报警（性质：超过阈值必有告警）。
        let deadline = std::time::Instant::now() + Duration::from_secs(3);
        while log.lock().unwrap().is_empty() {
            assert!(
                std::time::Instant::now() < deadline,
                "静默超阈值后 3s 内未产生任何告警"
            );
            thread::sleep(Duration::from_millis(20));
        }
        let msg = log.lock().unwrap()[0].clone();
        assert!(msg.contains("静默"), "告警应描述静默事实: {msg}");

        // 活性过期期间标记文件冻结（外部观察者看到 mtime 停走 = 判死的依据）。
        let marker = log_dir.join(LIVENESS_MARKER_FILENAME);
        assert!(!marker.exists(), "静默期不得刷新活性标记文件");
    }

    #[test]
    fn watchdog_stays_silent_while_writes_flow() {
        let (_dir, log_dir) = temp_log_dir();
        let liveness = LogLiveness::new();
        let (sink, log) = collecting_sink();

        spawn_silence_watchdog(
            liveness.clone(),
            &log_dir,
            WatchdogConfig {
                // 阈值 2000ms（打点周期 20ms 的 100 倍）：llvm-cov 插桩 +
                // CI 负载 runner 下 thread::sleep 显著拉长，200ms 阈值
                // （10 倍余量）曾把活跃期误判静默——静默误报路径的余量
                // 按最慢环境给足，语义（活跃期不误报）不变。
                silence_threshold: Duration::from_millis(2000),
                check_interval: Duration::from_millis(50),
            },
            sink,
            None,
        );

        // 模拟常态心跳（周期 << 阈值）：活跃窗口内持续打点，无任何告警。
        for _ in 0..10 {
            thread::sleep(Duration::from_millis(20));
            liveness.record_ok_write();
        }
        assert!(
            log.lock().unwrap().is_empty(),
            "写面活跃时不得误报: {:?}",
            log.lock().unwrap()
        );

        // 活跃期标记文件被刷新，内容含进程号与打点时刻（字面值断言 + 性质断言：
        // 打点时刻与当前钟差必须小于 2 个阈值）。读侧轮询等待非空内容——
        // watchdog 是异步线程、写 marker 为先截断后写入的非原子两步，CI 慢机
        // 下同步直读会撞进截断窗口读到空文件（2026-11-06 CI 实证 len=0），
        // 与 wait_evidence_file 同款防竞态。
        let marker = log_dir.join(LIVENESS_MARKER_FILENAME);
        let deadline = std::time::Instant::now() + Duration::from_secs(3);
        let content = loop {
            if let Ok(c) = std::fs::read_to_string(&marker) {
                if !c.trim().is_empty() {
                    break c;
                }
            }
            assert!(
                std::time::Instant::now() < deadline,
                "3s 内 marker 未写出非空内容: {}",
                marker.display()
            );
            thread::sleep(Duration::from_millis(10));
        };
        let fields: Vec<&str> = content.split_whitespace().collect();
        assert_eq!(fields.len(), 2, "标记文件应为 <pid> <epoch_ms>: {content}");
        assert_eq!(
            fields[0],
            std::process::id().to_string(),
            "标记文件首字段应为进程号"
        );
        let ok_ms: i64 = fields[1].parse().expect("epoch ms 可解析");
        let drift = now_epoch_ms() - ok_ms;
        assert!(
            (0..2000).contains(&drift),
            "打点时刻距当前钟差 {drift}ms 超出 [0, 2000)"
        );
    }

    #[test]
    fn persistent_silence_realarms_at_rate_limit_not_per_tick() {
        let (_dir, log_dir) = temp_log_dir();
        let liveness = LogLiveness::new();
        backdate(&liveness, 60_000);
        let (sink, log) = collecting_sink();

        spawn_silence_watchdog(
            liveness,
            &log_dir,
            WatchdogConfig {
                silence_threshold: Duration::from_millis(200),
                check_interval: Duration::from_millis(50),
            },
            sink,
            None,
        );

        // 持续静默 ~1s：应重报（非一次性），且限频生效（非每 tick 刷屏）。
        thread::sleep(Duration::from_millis(1_000));
        let stamps: Vec<String> = log.lock().unwrap().drain(..).collect();
        assert!(
            stamps.len() >= 2,
            "持续静默必须重报（限频窗口 {}ms 内得 {} 条）",
            200,
            stamps.len()
        );
        assert!(
            stamps.len() <= 8,
            "告警数 {} 超出限频上界（每 200ms 至多 1 条，1s 内 ≤6 条）",
            stamps.len()
        );
    }

    // --- F3 分池：单类告警风暴不挤占他类 ---

    #[test]
    fn alarm_rate_pool_gates_within_interval_and_rearms_after() {
        let mut pool = AlarmRatePool::new(100);
        assert!(pool.acquire(1_000), "首报直接放行");
        assert!(!pool.acquire(1_050), "窗口内再取必须被拒");
        assert!(!pool.acquire(1_099), "窗口边界前一刻仍被拒");
        assert!(
            pool.acquire(1_100),
            "窗口过后重新放行（周期性重报而非一次性）"
        );
    }

    #[test]
    fn drop_storm_does_not_starve_silence_alarms() {
        let (_dir, log_dir) = temp_log_dir();
        let liveness = LogLiveness::new();
        backdate(&liveness, 60_000); // 静默远超阈值
        let (sink, log) = collecting_sink();
        let (mut nb, _guard) = tracing_appender::non_blocking::NonBlockingBuilder::default()
            .buffered_lines_limit(1)
            .finish(StuckWriter);
        let drop_counter = nb.error_counter();
        spawn_silence_watchdog(liveness, &log_dir, fault_config(), sink, Some(drop_counter));

        // 持续 ~1.2s 的丢帧风暴（≥6 个 200ms 限频窗口）：丢帧计数全程推进，
        // 共享单池的旧实现里先检查的丢帧面会每个窗口都占用预算，静默告警
        // 被永久饿死（0 条）；分池后静默按自身配额周期性重报。
        let storm_end = std::time::Instant::now() + Duration::from_millis(1_200);
        let mut i = 0usize;
        while std::time::Instant::now() < storm_end {
            let _ = nb.write_all(format!("storm-{i}").as_bytes());
            i += 1;
            thread::sleep(Duration::from_millis(5));
        }

        let entries = log.lock().unwrap();
        let drops_count = entries.iter().filter(|m| m.contains("丢帧")).count();
        let silence_count = entries.iter().filter(|m| m.contains("静默")).count();
        assert!(drops_count >= 1, "风暴窗内丢帧告警应在案: {entries:?}");
        assert!(
            silence_count >= 2,
            "丢帧风暴不得挤占静默告警（分池配额，1.2s/200ms 窗应 ≥2 条，实测 {silence_count}）: {entries:?}"
        );
    }

    // --- F3 证据文件轮转：超限滚动、代数有界、最旧被覆盖 ---

    #[test]
    fn evidence_file_rotates_oversize_into_bounded_generations() {
        let (_dir, log_dir) = temp_log_dir();
        let (sink, log) = collecting_sink();
        let path = log_dir.join(LIVENESS_EVIDENCE_FILENAME);
        // 小限额驱动多轮滚动（40B 阈值 < 单行 ~50B：每次追加前必触发滚动）。
        for i in 0..12 {
            record_evidence_with_limits(&path, &format!("alarm-line-{i}"), &sink, 40, 2);
        }
        assert!(
            log.lock().unwrap().is_empty(),
            "正常滚动不得产生轮转/写入失败告警: {:?}",
            log.lock().unwrap()
        );

        // 代数有界：保留 2 代 → .1/.2 在，.3 不存在（超出即删，滚动覆盖最旧）。
        assert!(rotated_path(&path, 1).exists(), "第一代轮转文件应在");
        assert!(rotated_path(&path, 2).exists(), "第二代轮转文件应在");
        assert!(
            !rotated_path(&path, 3).exists(),
            "超出保留代数的最旧代必须被覆盖删除"
        );

        // 活跃文件滚动后回到单行盘面（性质：不随追加次数无界增长）。
        let active_len = std::fs::metadata(&path).unwrap().len();
        assert!(active_len < 200, "活跃文件应滚动重置，实测 {active_len}B");
        let total: u64 = [path.clone(), rotated_path(&path, 1), rotated_path(&path, 2)]
            .iter()
            .map(|p| std::fs::metadata(p).unwrap().len())
            .sum();
        assert!(total < 3 * 200, "证据面总盘面必须有界，实测 {total}B");

        // 内容覆盖面：最新事实在活跃文件；.2 只剩有界历史（最新行已被滚出）。
        let active = std::fs::read_to_string(&path).unwrap();
        assert!(
            active.contains("alarm-line-11"),
            "最新告警必须在活跃文件: {active}"
        );
        let oldest_kept = std::fs::read_to_string(rotated_path(&path, 2)).unwrap();
        assert!(
            !oldest_kept.contains("alarm-line-11"),
            "最旧保留代必须是更早的历史行: {oldest_kept}"
        );
    }

    #[test]
    fn evidence_rotation_first_append_and_keep_zero_edge() {
        let (_dir, log_dir) = temp_log_dir();
        let (sink, log) = collecting_sink();
        let path = log_dir.join(LIVENESS_EVIDENCE_FILENAME);

        // 首次落盘（文件不存在）：不轮转、直接追加。
        record_evidence_with_limits(&path, "first", &sink, 1, 2);
        assert!(path.exists(), "首次落盘必须创建证据文件");
        assert!(log.lock().unwrap().is_empty(), "首次落盘不得报轮转失败");

        // keep=0 防御形态：滚动 = 清空活跃文件（不留任何代文件）。
        std::fs::write(&path, b"0123456789").unwrap();
        rotate_evidence_if_oversized(&path, 5, 0).unwrap();
        assert!(!path.exists(), "keep=0 滚动应删除活跃文件");
    }

    // --- 注入式故障端到端：写手死亡 / 通道满，调用方都不卡死（真实线程 + 真实
    // non_blocking 通道，故障经注入的写手实现承载，非零延迟 mock） ---

    /// 写手死亡形态：IO 层每写必败（worker 吞错继续消费，打点永不前进）。
    struct DeadWriter;
    impl io::Write for DeadWriter {
        fn write(&mut self, _buf: &[u8]) -> io::Result<usize> {
            Err(io::Error::other("writer dead"))
        }
        fn flush(&mut self) -> io::Result<()> {
            Err(io::Error::other("writer dead"))
        }
    }

    /// 通道满形态：文件 IO 永久卡死（worker 停滞，队列涨满后 lossy 丢帧）。
    struct StuckWriter;
    impl io::Write for StuckWriter {
        fn write(&mut self, _buf: &[u8]) -> io::Result<usize> {
            loop {
                // 无 unpark 来源的真实阻塞注入；park 可能虚假唤醒，循环再停。
                thread::park();
            }
        }
        fn flush(&mut self) -> io::Result<()> {
            loop {
                thread::park();
            }
        }
    }

    fn fault_config() -> WatchdogConfig {
        WatchdogConfig {
            silence_threshold: Duration::from_millis(200),
            check_interval: Duration::from_millis(20),
        }
    }

    #[test]
    fn dead_writer_callers_never_block_and_silence_lands_in_evidence_file() {
        let (_dir, log_dir) = temp_log_dir();
        let liveness = LogLiveness::new();
        backdate(&liveness, 60_000);
        let (sink, log) = collecting_sink();
        let (mut nb, _guard) = tracing_appender::non_blocking(LivenessWriter::new(
            DeadWriter,
            liveness.clone(),
            sink.clone(),
            Duration::from_millis(200),
        ));

        // 调用方不卡死：写手死亡表现为后台静默，入队调用立即返回 Ok。
        let started = std::time::Instant::now();
        nb.write_all(b"caller must not block")
            .expect("入队必须成功（lossy 通道不反压调用方）");
        assert!(
            started.elapsed() < Duration::from_secs(2),
            "写手死亡时调用方被阻塞 {:?}",
            started.elapsed()
        );

        spawn_silence_watchdog(liveness, &log_dir, fault_config(), sink, None);

        // 静默告警照常触发（真实周期线程）：静默 + 写失败两类报告都应在案。
        let deadline = std::time::Instant::now() + Duration::from_secs(3);
        loop {
            let entries = log.lock().unwrap();
            let has_silence = entries.iter().any(|m| m.contains("静默"));
            let has_write_err = entries.iter().any(|m| m.contains("writer dead"));
            drop(entries);
            if has_silence && has_write_err {
                break;
            }
            assert!(
                std::time::Instant::now() < deadline,
                "3s 内未凑齐静默告警与写失败报告: {log:?}"
            );
            thread::sleep(Duration::from_millis(10));
        }

        // 证据文件独立于 stderr 落盘（装机态 stderr 被丢弃后的权威追责面）。
        let content = wait_evidence_file(&log_dir);
        assert!(content.contains("静默"), "证据应描述静默事实: {content}");
        assert!(
            content.contains(now_epoch_ms().to_string().as_str())
                || chrono::DateTime::parse_from_rfc3339(
                    content.split(' ').next().expect("证据行带时间戳前缀"),
                )
                .is_ok(),
            "证据行必须带可解析时间戳前缀: {content}"
        );
    }

    #[test]
    fn saturated_channel_drops_without_blocking_and_watchdog_alarms_drops() {
        let (_dir, log_dir) = temp_log_dir();
        let liveness = LogLiveness::new();
        let (sink, log) = collecting_sink();
        let (mut nb, _guard) = tracing_appender::non_blocking::NonBlockingBuilder::default()
            .buffered_lines_limit(2)
            .finish(StuckWriter);

        let drop_counter = nb.error_counter();
        // 容量 2 + worker 永久停滞：10 连写全部立即返回 Ok（lossy 不反压）。
        let started = std::time::Instant::now();
        for i in 0..10 {
            nb.write_all(format!("line-{i}").as_bytes())
                .expect("入队必须成功（通道满只丢不阻塞）");
        }
        assert!(
            started.elapsed() < Duration::from_secs(2),
            "通道满时调用方被阻塞 {:?}",
            started.elapsed()
        );
        assert!(
            drop_counter.dropped_lines() >= 1,
            "超容量后必须产生丢帧计数（性质：容量之外必丢）"
        );

        spawn_silence_watchdog(liveness, &log_dir, fault_config(), sink, Some(drop_counter));

        // 丢帧计数前进 → 看门狗独立于静默阈值告警并落证据文件。
        let deadline = std::time::Instant::now() + Duration::from_secs(3);
        loop {
            let hit = log.lock().unwrap().iter().any(|m| m.contains("丢帧"));
            if hit {
                break;
            }
            assert!(
                std::time::Instant::now() < deadline,
                "3s 内未产生丢帧告警: {log:?}"
            );
            thread::sleep(Duration::from_millis(10));
        }
        let content = wait_evidence_file(&log_dir);
        assert!(
            content.contains("丢帧"),
            "证据文件应记载丢帧事实: {content}"
        );
    }

    // --- LivenessWriter：成功打点 / 失败显式报告且限频 ---

    struct FailWrite;
    impl io::Write for FailWrite {
        fn write(&mut self, _buf: &[u8]) -> io::Result<usize> {
            Err(io::Error::other("disk gone"))
        }
        fn flush(&mut self) -> io::Result<()> {
            Err(io::Error::other("disk gone"))
        }
    }

    #[test]
    fn writer_records_ok_writes_and_swallows_nothing_on_error() {
        let liveness = LogLiveness::new();
        backdate(&liveness, 60_000);
        let (sink, log) = collecting_sink();
        let mut writer = LivenessWriter::new(
            FailWrite,
            liveness.clone(),
            sink,
            Duration::from_millis(200),
        );

        let err = writer.write(b"hello").expect_err("内层失败必须上抛");
        assert_eq!(err.to_string(), "disk gone");

        // 失败必须显式报告（限频窗口内只报一次），不能静默吞掉。
        let deadline = std::time::Instant::now() + Duration::from_secs(1);
        while log.lock().unwrap().is_empty() {
            assert!(std::time::Instant::now() < deadline, "写失败未产生任何报告");
            thread::sleep(Duration::from_millis(10));
        }
        let msg = log.lock().unwrap()[0].clone();
        assert!(msg.contains("disk gone"), "报告应带错误细节: {msg}");

        // 限频：窗口内连续失败只报一次。
        writer.write(b"again").expect_err("仍失败");
        writer.flush().expect_err("仍失败");
        assert_eq!(log.lock().unwrap().len(), 1, "限频窗口内重复失败不得刷屏");
    }

    #[test]
    fn writer_error_report_rearms_after_rate_limit_window() {
        let liveness = LogLiveness::new();
        let (sink, log) = collecting_sink();
        let mut writer = LivenessWriter::new(FailWrite, liveness, sink, Duration::from_millis(80));

        writer.write(b"first").expect_err("失败");
        thread::sleep(Duration::from_millis(120));
        writer.write(b"second").expect_err("失败");

        let deadline = std::time::Instant::now() + Duration::from_secs(1);
        while log.lock().unwrap().len() < 2 {
            assert!(
                std::time::Instant::now() < deadline,
                "限频窗口过后未重报（一次性报告不可接受）"
            );
            thread::sleep(Duration::from_millis(10));
        }
    }

    #[test]
    fn writer_ok_write_advances_liveness() {
        let liveness = LogLiveness::new();
        backdate(&liveness, 60_000);
        let (sink, _log) = collecting_sink();
        let mut writer =
            LivenessWriter::new(Vec::new(), liveness.clone(), sink, Duration::from_secs(60));

        writer.write_all(b"ok").expect("成功写");
        assert!(
            liveness.ms_since_ok_write() < 1_000,
            "成功写后活性打点必须前进（实测 {}ms）",
            liveness.ms_since_ok_write()
        );
    }
}
