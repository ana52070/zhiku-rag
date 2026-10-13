use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct ScheduleConfig {
    pub kind: String,
    #[serde(default = "default_time")]
    pub time: String,
    #[serde(default)]
    pub weekdays: Vec<u8>,
    #[serde(default = "default_interval")]
    pub interval_hours: u32,
    #[serde(default)]
    pub cron: String,
    #[serde(default = "default_timezone")]
    pub timezone: String,
}
fn default_time() -> String {
    "02:00".into()
}
fn default_timezone() -> String {
    "Asia/Shanghai".into()
}
fn default_interval() -> u32 {
    24
}

pub fn next_run(cfg: &ScheduleConfig, now: DateTime<Utc>) -> Result<DateTime<Utc>, String> {
    use std::str::FromStr;
    let timezone = chrono_tz::Tz::from_str(&cfg.timezone).map_err(|_| "时区名称无效。")?;
    if cfg.kind == "interval" {
        if !(1..=8760).contains(&cfg.interval_hours) {
            return Err("间隔必须是 1 至 8760 小时。".into());
        }
        return Ok(now + chrono::Duration::hours(cfg.interval_hours as i64));
    }
    let pattern = match cfg.kind.as_str() {
        "daily" | "weekly" => {
            let time = chrono::NaiveTime::parse_from_str(&cfg.time, "%H:%M")
                .map_err(|_| "时间格式须为 HH:MM。")?;
            use chrono::Timelike;
            let weekdays = if cfg.kind == "weekly" {
                if cfg.weekdays.is_empty() || cfg.weekdays.iter().any(|d| *d > 6) {
                    return Err("请至少选择一个星期，取值 0 至 6。".into());
                }
                cfg.weekdays
                    .iter()
                    .map(|d| d.to_string())
                    .collect::<Vec<_>>()
                    .join(",")
            } else {
                "*".into()
            };
            format!("{} {} * * {}", time.minute(), time.hour(), weekdays)
        }
        "cron" => {
            if cfg.cron.split_whitespace().count() != 5 {
                return Err("Cron 须为五字段：分 时 日 月 星期。".into());
            }
            cfg.cron.clone()
        }
        _ => return Err("不支持的调度类型。".into()),
    };
    let cron = croner::Cron::from_str(&pattern).map_err(|_| "Cron 表达式无效。")?;
    cron.find_next_occurrence(&now.with_timezone(&timezone), false)
        .map(|date| date.with_timezone(&Utc))
        .map_err(|_| "该规则没有可执行的下一次时间。".into())
}

#[cfg(test)]
mod tests {
    use super::*;
    fn cfg(kind: &str) -> ScheduleConfig {
        ScheduleConfig {
            kind: kind.into(),
            time: "02:00".into(),
            weekdays: vec![1],
            interval_hours: 24,
            cron: "0 2 * * *".into(),
            timezone: "Asia/Shanghai".into(),
        }
    }
    #[test]
    fn daily_uses_local_timezone() {
        let now = "2026-10-13T17:00:00Z".parse::<DateTime<Utc>>().unwrap();
        assert_eq!(
            next_run(&cfg("daily"), now).unwrap().to_rfc3339(),
            "2026-10-13T18:00:00+00:00"
        );
    }
    #[test]
    fn weekly_and_standard_cron() {
        let now = "2026-10-13T17:00:00Z".parse::<DateTime<Utc>>().unwrap();
        assert_eq!(
            next_run(&cfg("weekly"), now).unwrap().to_rfc3339(),
            "2026-10-18T18:00:00+00:00"
        );
        assert_eq!(
            next_run(&cfg("cron"), now).unwrap(),
            next_run(&cfg("daily"), now).unwrap()
        );
    }
    #[test]
    fn invalid_schedule_rejected() {
        let mut value = cfg("daily");
        value.time = "25:00".into();
        assert!(next_run(&value, Utc::now()).is_err());
        value = cfg("cron");
        value.cron = "* * * * * *".into();
        assert!(next_run(&value, Utc::now()).is_err());
        value = cfg("interval");
        value.interval_hours = 0;
        assert!(next_run(&value, Utc::now()).is_err());
        value = cfg("daily");
        value.timezone = "unknown".into();
        assert!(next_run(&value, Utc::now()).is_err());
    }
}
