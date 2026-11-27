use std::sync::Arc;

use icaloxide_core::datetime::{CalDate, CalDateTime, CalTime};
use icaloxide_core::error::ParseOptions;
use icaloxide_core::{Calendar, ComponentKind};
use icaloxide_rrule::{
    expand_rrule, CalendarQuery, IterationBudget, LocalDateTime, QueryOptions, RRule, RRuleError,
};
use smallvec::smallvec;

fn ldt(y: i16, m: u8, d: u8, hh: u8, mm: u8, ss: u8) -> LocalDateTime {
    LocalDateTime::new(
        CalDate::new(y, m, d).unwrap(),
        CalTime::new(hh, mm, ss).unwrap(),
    )
}

fn utc_ts(y: i16, m: u8, d: u8, hh: u8, mm: u8, ss: u8) -> i64 {
    ldt(y, m, d, hh, mm, ss).to_naive_timestamp()
}

#[test]
fn yearly_monthly_and_bysetpos_workday() {
    // Last weekday of each month in Q1 2026
    let rule = RRule::parse("FREQ=MONTHLY;BYDAY=MO,TU,WE,TH,FR;BYSETPOS=-1;COUNT=4").unwrap();
    let res = expand_rrule(
        &rule,
        ldt(2026, 1, 1, 9, 0, 0),
        None,
        None,
        None,
        IterationBudget::default(),
    )
    .unwrap();
    assert_eq!(
        res,
        vec![
            ldt(2026, 1, 30, 9, 0, 0), // Jan 31 is Saturday -> Fri Jan 30
            ldt(2026, 2, 27, 9, 0, 0), // Feb 28 is Saturday -> Fri Feb 27
            ldt(2026, 3, 31, 9, 0, 0), // Mar 31 is Tuesday
            ldt(2026, 4, 30, 9, 0, 0), // Apr 30 is Thursday
        ]
    );

    // YEARLY + BYWEEKNO + BYDAY
    let wk_rule = RRule::parse("FREQ=YEARLY;BYWEEKNO=1;BYDAY=MO;COUNT=2;WKST=MO").unwrap();
    let wk_res = expand_rrule(
        &wk_rule,
        ldt(2026, 1, 1, 8, 0, 0),
        None,
        None,
        None,
        IterationBudget::default(),
    )
    .unwrap();
    assert_eq!(wk_res.len(), 2);
    assert_eq!(wk_res[0].date, CalDate::new(2027, 1, 4).unwrap());

    // YEARLY + BYYEARDAY=-1
    let yd_rule = RRule::parse("FREQ=YEARLY;BYYEARDAY=-1;COUNT=2").unwrap();
    let yd_res = expand_rrule(
        &yd_rule,
        ldt(2024, 1, 1, 12, 0, 0),
        None,
        None,
        None,
        IterationBudget::default(),
    )
    .unwrap();
    assert_eq!(
        yd_res,
        vec![ldt(2024, 12, 31, 12, 0, 0), ldt(2025, 12, 31, 12, 0, 0)]
    );
}

#[test]
fn weekly_daily_and_subdaily_frequencies() {
    let weekly_rule = RRule::parse("FREQ=WEEKLY;INTERVAL=2;WKST=SU;BYDAY=SU,WE;COUNT=4").unwrap();
    let weekly_res = expand_rrule(
        &weekly_rule,
        ldt(2026, 11, 25, 10, 0, 0), // Wednesday
        None,
        None,
        None,
        IterationBudget::default(),
    )
    .unwrap();
    assert_eq!(
        weekly_res,
        vec![
            ldt(2026, 11, 25, 10, 0, 0), // Wed
            ldt(2026, 12, 6, 10, 0, 0),  // Sun (2 weeks from Sun Nov 22)
            ldt(2026, 12, 9, 10, 0, 0),  // Wed
            ldt(2026, 12, 20, 10, 0, 0), // Sun
        ]
    );

    let daily_rule = RRule::parse("FREQ=DAILY;INTERVAL=3;COUNT=2").unwrap();
    assert_eq!(
        expand_rrule(
            &daily_rule,
            ldt(2026, 1, 30, 9, 0, 0),
            None,
            None,
            None,
            IterationBudget::default(),
        )
        .unwrap(),
        vec![ldt(2026, 1, 30, 9, 0, 0), ldt(2026, 2, 2, 9, 0, 0)]
    );

    let hourly_rule = RRule::parse("FREQ=HOURLY;INTERVAL=3;COUNT=3").unwrap();
    assert_eq!(
        expand_rrule(
            &hourly_rule,
            ldt(2026, 1, 1, 22, 0, 0),
            None,
            None,
            None,
            IterationBudget::default(),
        )
        .unwrap(),
        vec![
            ldt(2026, 1, 1, 22, 0, 0),
            ldt(2026, 1, 2, 1, 0, 0),
            ldt(2026, 1, 2, 4, 0, 0),
        ]
    );

    let minutely_rule = RRule::parse("FREQ=MINUTELY;INTERVAL=45;COUNT=2").unwrap();
    assert_eq!(
        expand_rrule(
            &minutely_rule,
            ldt(2026, 1, 1, 0, 30, 0),
            None,
            None,
            None,
            IterationBudget::default(),
        )
        .unwrap(),
        vec![ldt(2026, 1, 1, 0, 30, 0), ldt(2026, 1, 1, 1, 15, 0)]
    );

    let secondly_rule = RRule::parse("FREQ=SECONDLY;INTERVAL=30;COUNT=3").unwrap();
    assert_eq!(
        expand_rrule(
            &secondly_rule,
            ldt(2026, 1, 1, 0, 0, 15),
            None,
            None,
            None,
            IterationBudget::default(),
        )
        .unwrap(),
        vec![
            ldt(2026, 1, 1, 0, 0, 15),
            ldt(2026, 1, 1, 0, 0, 45),
            ldt(2026, 1, 1, 0, 1, 15),
        ]
    );
}

#[test]
fn fast_forward_56_years_and_dos_budget_guard() {
    // 1970 -> 2026 weekly fast-forward should check only ~2 periods, not 2,900+
    let rule = RRule::parse("FREQ=WEEKLY;BYDAY=MO,FR").unwrap();
    let tight_budget = IterationBudget {
        max_periods: 20,
        max_empty_periods: 5,
    };
    let res = expand_rrule(
        &rule,
        ldt(1970, 1, 2, 9, 0, 0),
        None,
        Some(ldt(2026, 6, 1, 0, 0, 0)),
        Some(ldt(2026, 6, 8, 0, 0, 0)),
        tight_budget,
    )
    .unwrap();
    assert_eq!(
        res,
        vec![ldt(2026, 6, 1, 9, 0, 0), ldt(2026, 6, 5, 9, 0, 0)]
    );

    // Impossible Feb 30 rule must terminate with BudgetExceeded
    let impossible = RRule::parse("FREQ=DAILY;BYMONTH=2;BYMONTHDAY=30").unwrap();
    let err = expand_rrule(
        &impossible,
        ldt(2026, 1, 1, 0, 0, 0),
        None,
        None,
        None,
        IterationBudget {
            max_periods: 10_000,
            max_empty_periods: 500,
        },
    )
    .unwrap_err();
    assert_eq!(err, RRuleError::BudgetExceeded);
}

#[test]
fn dst_wall_clock_exdate_recurrence_id_and_thisandfuture() {
    let ics = "\
BEGIN:VCALENDAR\r\n\
VERSION:2.0\r\n\
PRODID:-//icaloxide//rrule-test//EN\r\n\
BEGIN:VEVENT\r\n\
UID:standup@icaloxide\r\n\
DTSTART;TZID=America/New_York:20260306T090000\r\n\
DURATION:PT30M\r\n\
RRULE:FREQ=DAILY;COUNT=6\r\n\
EXDATE;TZID=America/New_York:20260307T090000\r\n\
EXDATE:20260310T130000Z\r\n\
SUMMARY:Standup\r\n\
END:VEVENT\r\n\
BEGIN:VEVENT\r\n\
UID:standup@icaloxide\r\n\
RECURRENCE-ID;TZID=America/New_York:20260309T090000\r\n\
DTSTART;TZID=America/New_York:20260309T110000\r\n\
DURATION:PT45M\r\n\
SUMMARY:Standup (Moved)\r\n\
END:VEVENT\r\n\
BEGIN:VEVENT\r\n\
UID:weekly-shift@icaloxide\r\n\
DTSTART:20260101T100000Z\r\n\
DURATION:PT1H\r\n\
RRULE:FREQ=WEEKLY;COUNT=4\r\n\
SUMMARY:Old Time\r\n\
END:VEVENT\r\n\
BEGIN:VEVENT\r\n\
UID:weekly-shift@icaloxide\r\n\
RECURRENCE-ID;RANGE=THISANDFUTURE:20260115T100000Z\r\n\
DTSTART:20260115T140000Z\r\n\
DURATION:PT1H\r\n\
SUMMARY:New Time\r\n\
END:VEVENT\r\n\
END:VCALENDAR\r\n";

    let cal =
        Arc::new(Calendar::parse_with_options(ics.as_bytes(), &ParseOptions::strict()).unwrap());
    let query = CalendarQuery::new(Arc::clone(&cal), QueryOptions::default()).unwrap();

    // Standup checks across 2026-03-08 DST spring forward in America/New_York (EST -05:00 -> EDT -04:00)
    let occs = query
        .between(utc_ts(2026, 3, 6, 0, 0, 0), utc_ts(2026, 3, 12, 23, 59, 59))
        .unwrap();
    // Master dates: Mar 6 (EST 14:00Z), Mar 7 (EXDATE), Mar 8 (EDT 13:00Z!), Mar 9 (Moved to 11:00 EDT = 15:00Z), Mar 10 (EXDATE 13:00Z), Mar 11 (EDT 13:00Z)
    assert_eq!(occs.len(), 4);
    assert_eq!(occs[0].start_utc, utc_ts(2026, 3, 6, 14, 0, 0));
    assert_eq!(occs[1].start_utc, utc_ts(2026, 3, 8, 13, 0, 0)); // wall clock stayed 09:00 across DST!
    assert_eq!(occs[2].start_utc, utc_ts(2026, 3, 9, 15, 0, 0)); // moved override
    assert_eq!(
        cal.component(occs[2].component_id).unwrap().summary(),
        Some("Standup (Moved)")
    );
    assert_eq!(occs[3].start_utc, utc_ts(2026, 3, 11, 13, 0, 0));

    // THISANDFUTURE check: Jan 1 & Jan 8 at 10:00Z from master; Jan 15 & Jan 22 shifted to 14:00Z with "New Time"
    let tf_occs = query
        .between(utc_ts(2026, 1, 1, 0, 0, 0), utc_ts(2026, 1, 31, 0, 0, 0))
        .unwrap();
    assert_eq!(tf_occs.len(), 4);
    assert_eq!(tf_occs[0].start_utc, utc_ts(2026, 1, 1, 10, 0, 0));
    assert_eq!(tf_occs[1].start_utc, utc_ts(2026, 1, 8, 10, 0, 0));
    assert_eq!(tf_occs[2].start_utc, utc_ts(2026, 1, 15, 14, 0, 0));
    assert_eq!(tf_occs[3].start_utc, utc_ts(2026, 1, 22, 14, 0, 0));
    assert_eq!(
        cal.component(tf_occs[3].component_id).unwrap().summary(),
        Some("New Time")
    );
}

#[test]
fn vtodo_vfreebusy_valarm_and_streaming_after() {
    let ics = "\
BEGIN:VCALENDAR\r\n\
VERSION:2.0\r\n\
PRODID:-//icaloxide//multi-comp//EN\r\n\
BEGIN:VEVENT\r\n\
UID:inf-a\r\n\
DTSTART:20260101T100000Z\r\n\
DURATION:PT1H\r\n\
RRULE:FREQ=DAILY\r\n\
SUMMARY:Daily A\r\n\
BEGIN:VALARM\r\n\
ACTION:DISPLAY\r\n\
TRIGGER:-PT15M\r\n\
REPEAT:1\r\n\
DURATION:PT5M\r\n\
DESCRIPTION:Soon\r\n\
END:VALARM\r\n\
END:VEVENT\r\n\
BEGIN:VEVENT\r\n\
UID:inf-b\r\n\
DTSTART:20260101T080000Z\r\n\
DURATION:PT30M\r\n\
RRULE:FREQ=DAILY;INTERVAL=2\r\n\
SUMMARY:Every 2d B\r\n\
END:VEVENT\r\n\
BEGIN:VTODO\r\n\
UID:todo-1\r\n\
DUE:20260102T170000Z\r\n\
SUMMARY:Submit Report\r\n\
END:VTODO\r\n\
BEGIN:VFREEBUSY\r\n\
UID:fb-1\r\n\
FREEBUSY:20260102T120000Z/PT2H\r\n\
END:VFREEBUSY\r\n\
END:VCALENDAR\r\n";

    let cal =
        Arc::new(Calendar::parse_with_options(ics.as_bytes(), &ParseOptions::strict()).unwrap());

    // Streaming after() over two infinite VEVENTs
    let ev_query = Arc::new(CalendarQuery::new(Arc::clone(&cal), QueryOptions::default()).unwrap());
    let first_five: Vec<_> = ev_query
        .after(utc_ts(2026, 1, 1, 0, 0, 0))
        .take(5)
        .map(Result::unwrap)
        .collect();
    let starts: Vec<CalDateTime> = first_five.iter().map(|o| o.start_local.clone()).collect();
    assert_eq!(
        starts,
        vec![
            CalDateTime::parse_bytes(b"20260101T080000Z", None).unwrap(), // B day 1
            CalDateTime::parse_bytes(b"20260101T100000Z", None).unwrap(), // A day 1
            CalDateTime::parse_bytes(b"20260102T100000Z", None).unwrap(), // A day 2
            CalDateTime::parse_bytes(b"20260103T080000Z", None).unwrap(), // B day 3
            CalDateTime::parse_bytes(b"20260103T100000Z", None).unwrap(), // A day 3
        ]
    );

    // VALARM + VTODO + VFREEBUSY query
    let multi_opts = QueryOptions {
        components: smallvec![
            ComponentKind::VTodo,
            ComponentKind::VFreeBusy,
            ComponentKind::VAlarm
        ],
        ..QueryOptions::default()
    };
    let multi_q = CalendarQuery::new(Arc::clone(&cal), multi_opts).unwrap();
    let multi_occs = multi_q
        .between(utc_ts(2026, 1, 2, 0, 0, 0), utc_ts(2026, 1, 2, 23, 59, 59))
        .unwrap();
    // Expect: 2 VALARMs (09:45Z and 09:50Z), 1 VFREEBUSY (12:00Z..14:00Z), 1 VTODO (17:00Z)
    assert_eq!(multi_occs.len(), 4);
    assert_eq!(multi_occs[0].kind, ComponentKind::VAlarm);
    assert_eq!(multi_occs[0].start_utc, utc_ts(2026, 1, 2, 9, 45, 0));
    assert_eq!(multi_occs[1].kind, ComponentKind::VAlarm);
    assert_eq!(multi_occs[1].start_utc, utc_ts(2026, 1, 2, 9, 50, 0));
    assert_eq!(multi_occs[2].kind, ComponentKind::VFreeBusy);
    assert_eq!(multi_occs[3].kind, ComponentKind::VTodo);
}

#[test]
fn rdate_period_thisandfuture_split_skip_bad_and_valarm_after() {
    let ics = "\
BEGIN:VCALENDAR\r\n\
VERSION:2.0\r\n\
PRODID:-//icaloxide//v02-test//EN\r\n\
BEGIN:VEVENT\r\n\
UID:rdate-per\r\n\
DTSTART:20260101T100000Z\r\n\
DURATION:PT1H\r\n\
RDATE;VALUE=PERIOD:20260105T120000Z/PT3H\r\n\
SUMMARY:With Period RDATE\r\n\
BEGIN:VALARM\r\n\
ACTION:DISPLAY\r\n\
TRIGGER:-PT30M\r\n\
DESCRIPTION:Alarm\r\n\
END:VALARM\r\n\
END:VEVENT\r\n\
BEGIN:VEVENT\r\n\
UID:split-tf\r\n\
DTSTART:20260201T090000Z\r\n\
DURATION:PT1H\r\n\
RRULE:FREQ=WEEKLY;COUNT=4\r\n\
SUMMARY:Phase A\r\n\
END:VEVENT\r\n\
BEGIN:VEVENT\r\n\
UID:split-tf\r\n\
RECURRENCE-ID;RANGE=THISANDFUTURE:20260215T090000Z\r\n\
DTSTART:20260215T140000Z\r\n\
DURATION:PT2H\r\n\
SUMMARY:Phase B Shifted\r\n\
END:VEVENT\r\n\
BEGIN:VEVENT\r\n\
UID:bad-series\r\n\
DTSTART:20260101T000000Z\r\n\
RRULE:FREQ=INVALID_FREQ\r\n\
SUMMARY:Broken\r\n\
END:VEVENT\r\n\
BEGIN:VAVAILABILITY\r\n\
UID:avail-container\r\n\
BEGIN:AVAILABLE\r\n\
UID:avail-win\r\n\
DTSTART:20260110T090000Z\r\n\
DTEND:20260110T170000Z\r\n\
RRULE:FREQ=DAILY;COUNT=2\r\n\
SUMMARY:Office Hours\r\n\
END:AVAILABLE\r\n\
END:VAVAILABILITY\r\n\
END:VCALENDAR\r\n";

    let cal = Arc::new(Calendar::parse_str(ics).unwrap());
    let opts = QueryOptions {
        components: smallvec![ComponentKind::VEvent, ComponentKind::Available],
        skip_bad_series: true,
        ..QueryOptions::default()
    };
    let q = Arc::new(CalendarQuery::new(Arc::clone(&cal), opts).unwrap());
    assert_eq!(q.suppressed_errors().len(), 1);

    // Period RDATE preserves 3-hour custom span
    let jan = q
        .between(utc_ts(2026, 1, 1, 0, 0, 0), utc_ts(2026, 1, 15, 0, 0, 0))
        .unwrap();
    assert_eq!(jan.len(), 4); // Jan 1 (1h), Jan 5 (3h), Jan 10 (8h), Jan 11 (8h)
    assert_eq!(jan[1].end_utc - jan[1].start_utc, 3 * 3600);
    assert_eq!(jan[2].kind, ComponentKind::Available);

    // THISANDFUTURE shifted Feb 15 & Feb 22 to 14:00Z (2h) while keeping Feb 1 & Feb 8 at 09:00Z (1h)
    let feb = q
        .between(utc_ts(2026, 2, 1, 0, 0, 0), utc_ts(2026, 2, 28, 0, 0, 0))
        .unwrap();
    assert_eq!(feb.len(), 4);
    assert_eq!(feb[0].start_utc, utc_ts(2026, 2, 1, 9, 0, 0));
    assert_eq!(feb[1].start_utc, utc_ts(2026, 2, 8, 9, 0, 0));
    assert_eq!(feb[2].start_utc, utc_ts(2026, 2, 15, 14, 0, 0));
    assert_eq!(feb[2].end_utc - feb[2].start_utc, 2 * 3600);
    assert_eq!(feb[3].start_utc, utc_ts(2026, 2, 22, 14, 0, 0));

    // VALARM streaming after()
    let alarm_opts = QueryOptions {
        components: smallvec![ComponentKind::VAlarm],
        skip_bad_series: true,
        ..QueryOptions::default()
    };
    let alarm_q = Arc::new(CalendarQuery::new(Arc::clone(&cal), alarm_opts).unwrap());
    let alarms: Vec<_> = alarm_q
        .after(utc_ts(2026, 1, 1, 0, 0, 0))
        .map(Result::unwrap)
        .collect();
    assert_eq!(alarms.len(), 2);
    assert_eq!(alarms[0].start_utc, utc_ts(2026, 1, 1, 9, 30, 0));
    assert_eq!(alarms[1].start_utc, utc_ts(2026, 1, 5, 11, 30, 0));
}
