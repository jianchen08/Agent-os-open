//! 指标长期留存（监控设计 §十一 决策5：超 2h 落 SQLite）。
//!
//! 复用内核 SqliteStore 同款 rusqlite；本模块只加一张 metric_samples 表，
//! 不强依赖 engine crate 的 SqliteStore（避免 api→engine→store 循环）。
//! 调用方（bin/server）可传同一个 db 文件路径，两套表共存于一个库。
//!
//! schema（监控设计 §十一 决策5）：
//! ```sql
//! CREATE TABLE IF NOT EXISTS metric_samples (
//!   plugin_id   TEXT,
//!   name        TEXT,
//!   labels_hash INTEGER,
//!   bucket_ts   INTEGER,
//!   value       REAL,
//!   metric_type TEXT,
//!   PRIMARY KEY (plugin_id, name, labels_hash, bucket_ts)
//! );
//! ```

use parking_lot::Mutex;
use rusqlite::Connection;

use super::aggregator::{MetricSeriesView, MetricType};

/// 指标长期留存存储。
pub struct MetricsStore {
    conn: Mutex<Connection>,
}

impl MetricsStore {
    /// 打开（或创建）指标库。会建 metric_samples 表。
    pub fn open(path: &str) -> Result<Self, rusqlite::Error> {
        let conn = Connection::open(path)?;
        Self::init(&conn)?;
        Ok(Self {
            conn: Mutex::new(conn),
        })
    }

    /// 内存库（测试用）。
    pub fn open_memory() -> Result<Self, rusqlite::Error> {
        let conn = Connection::open_in_memory()?;
        Self::init(&conn)?;
        Ok(Self {
            conn: Mutex::new(conn),
        })
    }

    fn init(conn: &Connection) -> Result<(), rusqlite::Error> {
        conn.execute_batch(
            "CREATE TABLE IF NOT EXISTS metric_samples (
                plugin_id   TEXT NOT NULL,
                name        TEXT NOT NULL,
                labels_hash INTEGER NOT NULL,
                bucket_ts   INTEGER NOT NULL,
                value       REAL NOT NULL,
                metric_type TEXT NOT NULL,
                PRIMARY KEY (plugin_id, name, labels_hash, bucket_ts)
            );
            CREATE INDEX IF NOT EXISTS idx_metric_samples_ts ON metric_samples(bucket_ts);",
        )?;
        Ok(())
    }

    /// 把一批 series 的 sample 写入长期留存（upsert）。
    /// 用于 rollup 时把超 2h 的桶落盘。
    pub fn persist_series(
        &self,
        series: &[MetricSeriesView],
        labels_hash_fn: impl Fn(&super::Labels) -> u64,
    ) -> Result<usize, rusqlite::Error> {
        let conn = self.conn.lock();
        let mut count = 0usize;
        let sql = "INSERT OR REPLACE INTO metric_samples \
                   (plugin_id, name, labels_hash, bucket_ts, value, metric_type) \
                   VALUES (?1, ?2, ?3, ?4, ?5, ?6)";
        for s in series {
            let lh = labels_hash_fn(&s.labels) as i64;
            let typ = s.metric_type.as_str();
            for sample in &s.samples {
                conn.execute(
                    sql,
                    rusqlite::params![
                        s.plugin_id,
                        s.name,
                        lh,
                        sample.ts,
                        sample.value,
                        typ,
                    ],
                )?;
                count += 1;
            }
        }
        Ok(count)
    }

    /// 查询历史 sample（按 plugin/name/时间范围）。
    /// 返回 (bucket_ts, value, metric_type) 列表。
    pub fn query_history(
        &self,
        plugin_id: &str,
        name: &str,
        from_ts: i64,
        to_ts: i64,
    ) -> Result<Vec<HistorySample>, rusqlite::Error> {
        let conn = self.conn.lock();
        let mut stmt = conn.prepare(
            "SELECT bucket_ts, value, metric_type FROM metric_samples \
             WHERE plugin_id = ?1 AND name = ?2 AND bucket_ts >= ?3 AND bucket_ts <= ?4 \
             ORDER BY bucket_ts ASC",
        )?;
        let rows = stmt.query_map(
            rusqlite::params![plugin_id, name, from_ts, to_ts],
            |row| {
                Ok(HistorySample {
                    ts: row.get(0)?,
                    value: row.get(1)?,
                    metric_type: row.get(2)?,
                })
            },
        )?;
        let mut out = Vec::new();
        for r in rows {
            out.push(r?);
        }
        Ok(out)
    }

    /// 删除早于 cutoff 的 sample（清理）。
    pub fn evict_before(&self, cutoff_ts: i64) -> Result<usize, rusqlite::Error> {
        let conn = self.conn.lock();
        let n = conn.execute(
            "DELETE FROM metric_samples WHERE bucket_ts < ?1",
            rusqlite::params![cutoff_ts],
        )?;
        Ok(n)
    }
}

/// 历史查询返回的单个 sample。
#[derive(Debug, Clone)]
pub struct HistorySample {
    pub ts: i64,
    pub value: f64,
    pub metric_type: String,
}

impl HistorySample {
    pub fn metric_type(&self) -> Option<MetricType> {
        match self.metric_type.as_str() {
            "counter" => Some(MetricType::Counter),
            "gauge" => Some(MetricType::Gauge),
            "histogram" => Some(MetricType::Histogram),
            _ => None,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use super::super::aggregator::{labels_hash, Labels, MetricType, Sample};

    fn make_view(plugin: &str, name: &str, typ: MetricType, samples: Vec<(i64, f64)>) -> MetricSeriesView {
        MetricSeriesView {
            plugin_id: plugin.to_string(),
            name: name.to_string(),
            metric_type: typ,
            labels: Labels::new(),
            samples: samples
                .into_iter()
                .map(|(ts, value)| Sample { ts, value })
                .collect(),
            unit: None,
            help: None,
            latest: None,
            histogram: None,
        }
    }

    #[test]
    fn test_persist_and_query_history() {
        let store = MetricsStore::open_memory().unwrap();
        let s = make_view(
            "p1",
            "m",
            MetricType::Counter,
            vec![(1000, 10.0), (2000, 20.0), (3000, 30.0)],
        );
        let n = store.persist_series(&[s], |l| labels_hash(l)).unwrap();
        assert_eq!(n, 3);

        let hist = store.query_history("p1", "m", 1500, 2500).unwrap();
        assert_eq!(hist.len(), 1);
        assert_eq!(hist[0].ts, 2000);
        assert_eq!(hist[0].value, 20.0);
        assert_eq!(hist[0].metric_type, "counter");
    }

    #[test]
    fn test_persist_upsert_replaces() {
        let store = MetricsStore::open_memory().unwrap();
        let s1 = make_view("p1", "m", MetricType::Gauge, vec![(1000, 10.0)]);
        store.persist_series(&[s1], |l| labels_hash(l)).unwrap();
        // 同 (plugin,name,labels_hash,bucket_ts) 写入新值
        let s2 = make_view("p1", "m", MetricType::Gauge, vec![(1000, 99.0)]);
        store.persist_series(&[s2], |l| labels_hash(l)).unwrap();
        let hist = store.query_history("p1", "m", 0, 99999).unwrap();
        assert_eq!(hist.len(), 1, "upsert should replace not duplicate");
        assert_eq!(hist[0].value, 99.0);
    }

    #[test]
    fn test_evict_before() {
        let store = MetricsStore::open_memory().unwrap();
        let s = make_view(
            "p1",
            "m",
            MetricType::Counter,
            vec![(1000, 10.0), (2000, 20.0)],
        );
        store.persist_series(&[s], |l| labels_hash(l)).unwrap();
        let n = store.evict_before(1500).unwrap();
        assert_eq!(n, 1);
        let hist = store.query_history("p1", "m", 0, 99999).unwrap();
        assert_eq!(hist.len(), 1);
        assert_eq!(hist[0].ts, 2000);
    }

    #[test]
    fn test_query_history_no_match() {
        let store = MetricsStore::open_memory().unwrap();
        let hist = store.query_history("nonexistent", "m", 0, 99999).unwrap();
        assert!(hist.is_empty());
    }
}
