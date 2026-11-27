use std::collections::BTreeSet;

use compact_str::CompactString;
use jiff::tz::TimeZone;

use crate::datetime::{CalDate, CalDateTime, CalPeriod, CalTime, TimeZoneRef, UtcOffset};
use crate::model::{
    Calendar, ComponentId, ComponentKind, ParamName, Property, PropertyName, PropertyValue,
};
use crate::tz::TzRegistry;

/// Return all unique non-UTC `TZID` identifiers referenced anywhere in `cal`.
pub fn collect_used_tzids(cal: &Calendar) -> Vec<CompactString> {
    let mut referenced: BTreeSet<CompactString> = BTreeSet::new();
    let mut min_year: i16 = i16::MAX;
    let mut max_year: i16 = i16::MIN;
    collect_tzids_and_years(
        cal,
        cal.root_id(),
        &mut referenced,
        &mut min_year,
        &mut max_year,
    );
    referenced
        .into_iter()
        .filter(|tzid| {
            !tzid.eq_ignore_ascii_case("UTC")
                && !tzid.eq_ignore_ascii_case("Etc/UTC")
                && !tzid.eq_ignore_ascii_case("GMT")
        })
        .collect()
}

/// Return all referenced non-UTC `TZID`s that do not have a corresponding `VTIMEZONE` in `cal`.
pub fn collect_missing_tzids(cal: &Calendar) -> Vec<CompactString> {
    let mut existing: BTreeSet<CompactString> = BTreeSet::new();
    for vtz in cal.timezones() {
        if let Some(id) = vtz.property_text(&PropertyName::TzId) {
            existing.insert(CompactString::from(id));
        }
    }
    collect_used_tzids(cal)
        .into_iter()
        .filter(|tzid| !existing.contains(tzid))
        .collect()
}

/// Inspect all `TZID` references in `cal` and inject missing `VTIMEZONE` definitions
/// using the bundled IANA TZDB.
///
/// Existing `VTIMEZONE` definitions and UTC identifiers are skipped.
/// Returns the list of newly added `TZID`s in insertion order.
pub fn add_missing_timezones(cal: &mut Calendar) -> Vec<CompactString> {
    let registry = TzRegistry::from_calendar(cal);
    let mut existing: BTreeSet<CompactString> = BTreeSet::new();
    for vtz in cal.timezones() {
        if let Some(id) = vtz.property_text(&PropertyName::TzId) {
            existing.insert(CompactString::from(id));
        }
    }

    let mut referenced: BTreeSet<CompactString> = BTreeSet::new();
    let mut min_year: i16 = i16::MAX;
    let mut max_year: i16 = i16::MIN;

    collect_tzids_and_years(
        cal,
        cal.root_id(),
        &mut referenced,
        &mut min_year,
        &mut max_year,
    );

    if min_year > max_year {
        min_year = 2020;
        max_year = 2035;
    } else {
        min_year = min_year.saturating_sub(1);
        max_year = max_year.saturating_add(1);
    }

    let mut added_ids = Vec::new();
    let mut new_vtz_nodes = Vec::new();

    for tzid in referenced {
        if existing.contains(&tzid) {
            continue;
        }
        if tzid.eq_ignore_ascii_case("UTC")
            || tzid.eq_ignore_ascii_case("Etc/UTC")
            || tzid.eq_ignore_ascii_case("GMT")
        {
            continue;
        }

        let Some(canonical) = registry.resolve_canonical_iana(tzid.as_str()) else {
            continue;
        };
        if canonical == "UTC" {
            continue;
        }
        let Ok(iana_tz) = TimeZone::get(canonical.as_str()) else {
            continue;
        };

        let vtz_id = build_vtimezone_component(cal, &tzid, &iana_tz, min_year, max_year);
        new_vtz_nodes.push(vtz_id);
        added_ids.push(tzid);
    }

    // Insert newly generated VTIMEZONEs right after any existing children at the front of VCALENDAR.
    if !new_vtz_nodes.is_empty() {
        let root_id = cal.root_id();
        if let Some(root_node) = cal.arena.get_mut(root_id) {
            // `add_child_component` already appended `new_vtz_nodes` at the end; move them to the front.
            let tail_len = new_vtz_nodes.len();
            let keep_len = root_node.children.len().saturating_sub(tail_len);
            let mut reordered = Vec::with_capacity(root_node.children.len());
            reordered.extend_from_slice(&root_node.children[keep_len..]);
            reordered.extend_from_slice(&root_node.children[..keep_len]);
            root_node.children = reordered;
        }
    }

    added_ids
}

fn collect_tzids_and_years(
    cal: &Calendar,
    comp_id: ComponentId,
    tzids: &mut BTreeSet<CompactString>,
    min_year: &mut i16,
    max_year: &mut i16,
) {
    let Some(node) = cal.arena.get(comp_id) else {
        return;
    };

    if node.kind != ComponentKind::VTimezone {
        for prop in &node.properties {
            if prop.name == PropertyName::XWrTimeZone {
                if let Some(s) = prop.value.as_str() {
                    tzids.insert(CompactString::from(s.trim()));
                }
            }
            for param in &prop.params {
                if param.name == ParamName::TzId {
                    for val in &param.values {
                        tzids.insert(val.clone());
                    }
                }
            }
            record_value_tzids_and_years(&prop.value, tzids, min_year, max_year);
        }
    }

    let children = node.children.clone();
    for cid in children {
        collect_tzids_and_years(cal, cid, tzids, min_year, max_year);
    }
}

fn record_value_tzids_and_years(
    val: &PropertyValue,
    tzids: &mut BTreeSet<CompactString>,
    min_year: &mut i16,
    max_year: &mut i16,
) {
    match val {
        PropertyValue::Date(d) => update_year_bounds(d.year, min_year, max_year),
        PropertyValue::DateTime(dt) => {
            update_year_bounds(dt.date.year, min_year, max_year);
            if let TimeZoneRef::TzId(id) = &dt.tz {
                tzids.insert(id.clone());
            }
        }
        PropertyValue::DateList(list) => {
            for d in list {
                update_year_bounds(d.year, min_year, max_year);
            }
        }
        PropertyValue::DateTimeList(list) => {
            for dt in list {
                update_year_bounds(dt.date.year, min_year, max_year);
                if let TimeZoneRef::TzId(id) = &dt.tz {
                    tzids.insert(id.clone());
                }
            }
        }
        PropertyValue::Period(p) => record_period(p, tzids, min_year, max_year),
        PropertyValue::PeriodList(list) => {
            for p in list {
                record_period(p, tzids, min_year, max_year);
            }
        }
        _ => {}
    }
}

fn record_period(
    p: &CalPeriod,
    tzids: &mut BTreeSet<CompactString>,
    min_year: &mut i16,
    max_year: &mut i16,
) {
    match p {
        CalPeriod::Explicit { start, end } => {
            update_year_bounds(start.date.year, min_year, max_year);
            update_year_bounds(end.date.year, min_year, max_year);
            if let TimeZoneRef::TzId(id) = &start.tz {
                tzids.insert(id.clone());
            }
            if let TimeZoneRef::TzId(id) = &end.tz {
                tzids.insert(id.clone());
            }
        }
        CalPeriod::Duration { start, .. } => {
            update_year_bounds(start.date.year, min_year, max_year);
            if let TimeZoneRef::TzId(id) = &start.tz {
                tzids.insert(id.clone());
            }
        }
    }
}

#[inline]
fn update_year_bounds(year: i16, min_year: &mut i16, max_year: &mut i16) {
    if year < *min_year {
        *min_year = year;
    }
    if year > *max_year {
        *max_year = year;
    }
}

struct ObsSample {
    is_dst: bool,
    offset_from: i32,
    offset_to: i32,
    tzname: CompactString,
    dtstarts: Vec<CalDateTime>,
}

fn build_vtimezone_component(
    cal: &mut Calendar,
    tzid: &CompactString,
    tz: &TimeZone,
    min_year: i16,
    max_year: i16,
) -> ComponentId {
    let root_id = cal.root_id();
    let vtz_id = cal
        .add_child_component(root_id, ComponentKind::VTimezone)
        .expect("root component exists");

    if let Some(mut vtz) = cal.component_mut(vtz_id) {
        vtz.add_property(Property::new(
            PropertyName::TzId,
            PropertyValue::Text(tzid.clone()),
        ));
    }

    let groups = collect_observance_groups(tz, min_year, max_year);
    for group in groups {
        emit_observance_component(cal, vtz_id, group);
    }

    vtz_id
}

fn collect_observance_groups(tz: &TimeZone, min_year: i16, max_year: i16) -> Vec<ObsSample> {
    let start_ts = jiff::civil::DateTime::new(min_year, 1, 1, 0, 0, 0, 0)
        .ok()
        .and_then(|dt| tz.to_ambiguous_timestamp(dt).compatible().ok())
        .unwrap_or_else(jiff::Timestamp::now);
    let end_ts = jiff::civil::DateTime::new(max_year, 12, 31, 23, 59, 59, 0)
        .ok()
        .and_then(|dt| tz.to_ambiguous_timestamp(dt).compatible().ok())
        .unwrap_or_else(jiff::Timestamp::now);

    let mut groups: Vec<ObsSample> = Vec::new();
    for trans in tz.following(start_ts) {
        let trans_ts = trans.timestamp();
        if trans_ts > end_ts {
            break;
        }
        let prev_ts = trans_ts
            .checked_sub(jiff::Span::new().seconds(1))
            .unwrap_or(trans_ts);
        let offset_from = tz.to_offset(prev_ts).seconds();
        let offset_to = trans.offset().seconds();
        let is_dst = trans.dst().is_dst();
        let tzname = CompactString::from(trans.abbreviation());
        let local_before = timestamp_to_cal_datetime(trans_ts.as_second() + i64::from(offset_from));

        if let Some(group) = groups.iter_mut().find(|g| {
            g.is_dst == is_dst
                && g.offset_from == offset_from
                && g.offset_to == offset_to
                && g.tzname == tzname
        }) {
            group.dtstarts.push(local_before);
        } else {
            groups.push(ObsSample {
                is_dst,
                offset_from,
                offset_to,
                tzname,
                dtstarts: vec![local_before],
            });
        }
    }

    if groups.is_empty() {
        let info = tz.to_offset_info(start_ts);
        let off = info.offset().seconds();
        let last_trans = tz.preceding(start_ts).next();
        let prev_off = last_trans
            .as_ref()
            .and_then(|t| t.timestamp().checked_sub(jiff::Span::new().seconds(1)).ok())
            .map_or(off, |prev_ts| tz.to_offset(prev_ts).seconds());
        let dt_start = last_trans.map_or_else(
            || CalDateTime {
                date: CalDate {
                    year: 1970,
                    month: 1,
                    day: 1,
                },
                time: CalTime {
                    hour: 0,
                    minute: 0,
                    second: 0,
                },
                tz: TimeZoneRef::Floating,
            },
            |t| timestamp_to_cal_datetime(t.timestamp().as_second() + i64::from(prev_off)),
        );
        groups.push(ObsSample {
            is_dst: info.dst().is_dst(),
            offset_from: prev_off,
            offset_to: off,
            tzname: CompactString::from(info.abbreviation()),
            dtstarts: vec![dt_start],
        });
    }

    groups
}

fn emit_observance_component(cal: &mut Calendar, vtz_id: ComponentId, group: ObsSample) {
    let kind = if group.is_dst {
        ComponentKind::Daylight
    } else {
        ComponentKind::Standard
    };
    let obs_id = cal
        .add_child_component(vtz_id, kind)
        .expect("vtimezone exists");
    if let Some(mut obs) = cal.component_mut(obs_id) {
        obs.add_property(Property::new(
            PropertyName::DtStart,
            PropertyValue::DateTime(group.dtstarts[0].clone()),
        ));
        obs.add_property(Property::new(
            PropertyName::TzOffsetFrom,
            PropertyValue::UtcOffset(UtcOffset {
                seconds: group.offset_from,
            }),
        ));
        obs.add_property(Property::new(
            PropertyName::TzOffsetTo,
            PropertyValue::UtcOffset(UtcOffset {
                seconds: group.offset_to,
            }),
        ));
        if !group.tzname.is_empty() {
            obs.add_property(Property::new(
                PropertyName::TzName,
                PropertyValue::Text(group.tzname),
            ));
        }
        if group.dtstarts.len() > 1 {
            obs.add_property(Property::new(
                PropertyName::RDate,
                PropertyValue::DateTimeList(group.dtstarts[1..].to_vec()),
            ));
        }
    }
}

fn timestamp_to_cal_datetime(naive_seconds: i64) -> CalDateTime {
    let days = naive_seconds.div_euclid(86_400) as i32;
    let rem = naive_seconds.rem_euclid(86_400) as u32;
    let date = CalDate::from_days_since_epoch(days);
    let time = CalTime {
        hour: (rem / 3600) as u8,
        minute: ((rem % 3600) / 60) as u8,
        second: (rem % 60) as u8,
    };
    CalDateTime {
        date,
        time,
        tz: TimeZoneRef::Floating,
    }
}
