use icaloxide_core::datetime::{CalDateTime, CalPeriod, CalTime, TimeZoneRef};
use icaloxide_core::model::{
    ComponentId, ComponentKind, ComponentRef, ParamName, PropertyName, PropertyValue,
};
use icaloxide_core::tz::{CalendarTimeZone, TzRegistry};

use crate::query::Occurrence;
use crate::recurrence::{compute_end_local, resolve_datetime_with_fallback, SpanDuration};

/// Extract all `FREEBUSY` period entries from a `VFREEBUSY` component.
pub fn extract_freebusy_occurrences(
    comp: ComponentRef<'_>,
    reg: &TzRegistry,
    default_tz: &CalendarTimeZone,
) -> Vec<Occurrence> {
    let mut out = Vec::new();
    for prop in comp.properties_named(&PropertyName::FreeBusy) {
        match &prop.value {
            PropertyValue::Period(p) => {
                out.push(period_to_occ(comp.id(), p, reg, default_tz));
            }
            PropertyValue::PeriodList(list) => {
                for p in list {
                    out.push(period_to_occ(comp.id(), p, reg, default_tz));
                }
            }
            _ => {}
        }
    }
    out
}

fn period_to_occ(
    id: ComponentId,
    period: &CalPeriod,
    reg: &TzRegistry,
    default_tz: &CalendarTimeZone,
) -> Occurrence {
    match period {
        CalPeriod::Explicit { start, end } => {
            let (s_utc, _) = resolve_datetime_with_fallback(start, reg, default_tz);
            let (e_utc, _) = resolve_datetime_with_fallback(end, reg, default_tz);
            Occurrence {
                component_id: id,
                kind: ComponentKind::VFreeBusy,
                start_utc: s_utc,
                end_utc: e_utc.max(s_utc),
                start_local: start.clone(),
                end_local: end.clone(),
                is_all_day: false,
                recurrence_id_utc: None,
                parent_id: None,
            }
        }
        CalPeriod::Duration { start, duration } => {
            let (s_utc, tz) = resolve_datetime_with_fallback(start, reg, default_tz);
            let secs = duration.total_seconds().max(0);
            let (e_utc, end_local) =
                compute_end_local(start, s_utc, SpanDuration::Seconds(secs), &tz);
            Occurrence {
                component_id: id,
                kind: ComponentKind::VFreeBusy,
                start_utc: s_utc,
                end_utc: e_utc,
                start_local: start.clone(),
                end_local,
                is_all_day: false,
                recurrence_id_utc: None,
                parent_id: None,
            }
        }
    }
}

/// Expand `VALARM` subcomponents attached to a parent `VEVENT` or `VTODO` occurrence.
pub fn expand_alarms_for_parent(
    parent_comp: ComponentRef<'_>,
    parent_occ: &Occurrence,
    reg: &TzRegistry,
    default_tz: &CalendarTimeZone,
) -> Vec<Occurrence> {
    let mut out = Vec::new();
    for child in parent_comp.children() {
        if child.kind() != &ComponentKind::VAlarm {
            continue;
        }
        let Some(trigger) = child.property(&PropertyName::Trigger) else {
            continue;
        };

        let repeat = match child.property(&PropertyName::Repeat).map(|p| &p.value) {
            Some(PropertyValue::Integer(n)) if *n > 0 => *n as u32,
            _ => 0,
        };
        let repeat_dur_secs = match child.property(&PropertyName::Duration).map(|p| &p.value) {
            Some(PropertyValue::Duration(d)) => d.total_seconds().max(0),
            _ => 0,
        };

        match &trigger.value {
            PropertyValue::Duration(dur) => {
                let related_end = trigger
                    .param_first(&ParamName::Related)
                    .is_some_and(|r| r.eq_ignore_ascii_case("END"));
                let anchor_utc = if related_end {
                    parent_occ.end_utc
                } else {
                    parent_occ.start_utc
                };
                let base_utc = anchor_utc + dur.total_seconds();
                let count = if repeat > 0 && repeat_dur_secs > 0 {
                    repeat
                } else {
                    0
                };
                for idx in 0..=count {
                    let fire_utc = base_utc + i64::from(idx) * repeat_dur_secs;
                    let fire_local = utc_to_cal_datetime(fire_utc);
                    out.push(Occurrence {
                        component_id: child.id(),
                        kind: ComponentKind::VAlarm,
                        start_utc: fire_utc,
                        end_utc: fire_utc,
                        start_local: fire_local.clone(),
                        end_local: fire_local,
                        is_all_day: false,
                        recurrence_id_utc: Some(fire_utc),
                        parent_id: Some(parent_occ.component_id),
                    });
                }
            }
            PropertyValue::DateTime(dt) => {
                let (fire_utc, _) = resolve_datetime_with_fallback(dt, reg, default_tz);
                let count = if repeat > 0 && repeat_dur_secs > 0 {
                    repeat
                } else {
                    0
                };
                for idx in 0..=count {
                    let t_utc = fire_utc + i64::from(idx) * repeat_dur_secs;
                    let t_local = if idx == 0 {
                        dt.clone()
                    } else {
                        utc_to_cal_datetime(t_utc)
                    };
                    out.push(Occurrence {
                        component_id: child.id(),
                        kind: ComponentKind::VAlarm,
                        start_utc: t_utc,
                        end_utc: t_utc,
                        start_local: t_local.clone(),
                        end_local: t_local,
                        is_all_day: false,
                        recurrence_id_utc: Some(t_utc),
                        parent_id: Some(parent_occ.component_id),
                    });
                }
            }
            _ => {}
        }
    }
    out
}

fn utc_to_cal_datetime(utc_ts: i64) -> CalDateTime {
    let ldt = crate::rrule::LocalDateTime::from_naive_timestamp(utc_ts);
    CalDateTime {
        date: ldt.date,
        time: CalTime {
            hour: ldt.time.hour,
            minute: ldt.time.minute,
            second: ldt.time.second,
        },
        tz: TimeZoneRef::Utc,
    }
}
