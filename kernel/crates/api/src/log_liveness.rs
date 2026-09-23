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
//! 告警面为注入的闭包：生产接线为 stderr。注意装机部署 stderr 本身被丢弃
//! （electron `stdio: "ignore"`、bat supervisor → NUL），故标记文件才是权威
//! 外部判活面，stderr 报警服务于有终端可见性的入口（前台 sh、开发态）。
// @feature: FP-0.2.可观测性 日志面活性探测(BUG-71 裁决产物) | @ci: rust-test

use std::io;
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

/// 启动静默看门狗线程（进程存续期常驻；JoinHandle 即弃，线程自行 detach）。
///
/// 每个检查周期：
/// - 活性新鲜（距上次成功写 ≤ 阈值）→ 刷新标记文件（进程号 + 打点时刻）；
/// - 活性过期 → 按阈值限频向告警面报警（持续静默则周期性重报，非一次性）。
///
/// 标记文件写失败同样报警——它是外部判活的唯一权威面。
pub fn spawn_silence_watchdog(
    liveness: LogLiveness,
    log_dir: &Path,
    config: WatchdogConfig,
    alarm_sink: AlarmSink,
) {
    let marker_path = log_dir.join(LIVENESS_MARKER_FILENAME);
    let threshold_ms = config.silence_threshold.as_millis() as i64;
    thread::spawn(move || {
        let mut last_alarm_ms = 0i64;
        loop {
            thread::sleep(config.check_interval);
            let since_ok = liveness.ms_since_ok_write();
            if since_ok <= threshold_ms {
                if let Err(e) = write_liveness_marker(&marker_path, now_epoch_ms() - since_ok) {
                    let now = now_epoch_ms();
                    if now - last_alarm_ms >= threshold_ms.max(1) {
                        last_alarm_ms = now;
                        (alarm_sink)(format!(
                            "[log_liveness] 活性标记文件写入失败（外部判活面失效）: {e}"
                        ));
                    }
                }
            } else {
                let now = now_epoch_ms();
                if now - last_alarm_ms >= threshold_ms.max(1) {
                    last_alarm_ms = now;
                    (alarm_sink)(format!(
                        "[log_liveness] 内核日志已静默 {}s（阈值 {}s），写面疑似死亡；外部判活标记 {} 已停止刷新",
                        since_ok / 1000,
                        threshold_ms / 1000,
                        marker_path.display(),
                    ));
                }
            }
        }
    });
}

fn write_liveness_marker(path: &Path, ok_write_epoch_ms: i64) -> io::Result<()> {
    let content = format!("{} {}\n", std::process::id(), ok_write_epoch_ms);
    std::fs::write(path, content)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::VecDeque;
    use std::io::Write as _;
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

    // --- 看门狗：静默报警 / 活跃不报 / 限频重报 ---

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
                silence_threshold: Duration::from_millis(200),
                check_interval: Duration::from_millis(20),
            },
            sink,
        );

        // 模拟常态心跳（周期 < 阈值）：200ms 内持续打点，无任何告警。
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
        // 打点时刻与当前钟差必须小于 2 个阈值）。
        let marker = log_dir.join(LIVENESS_MARKER_FILENAME);
        let content = std::fs::read_to_string(&marker).expect("marker exists");
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
            (0..400).contains(&drift),
            "打点时刻距当前钟差 {drift}ms 超出 [0, 400)"
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
