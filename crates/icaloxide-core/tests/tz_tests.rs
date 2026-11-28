use icaloxide_core::datetime::{CalDate, CalTime};
use icaloxide_core::error::ParseOptions;
use icaloxide_core::tz::{
    parse_fixed_offset_tzid, resolve_iana_alias, strip_vendor_prefix, DstDisambiguation, TzError,
    TzRegistry,
};
use icaloxide_core::Calendar;

#[test]
fn windows_cldr_vendor_prefix_and_fixed_offsets() {
    assert_eq!(
        resolve_iana_alias("Turkey Standard Time"),
        Some("Europe/Istanbul")
    );
    assert_eq!(
        resolve_iana_alias("W. Europe Standard Time"),
        Some("Europe/Berlin")
    );
    assert_eq!(
        resolve_iana_alias("(UTC+03:00) Turkey Standard Time"),
        Some("Europe/Istanbul")
    );
    assert_eq!(
        resolve_iana_alias("India Standard Time"),
        Some("Asia/Kolkata")
    );
    assert_eq!(
        resolve_iana_alias("Eastern Standard Time"),
        Some("America/New_York")
    );

    assert_eq!(
        strip_vendor_prefix("/mozilla.org/20050126_1/Europe/Berlin"),
        Some("Europe/Berlin")
    );
    assert_eq!(
        strip_vendor_prefix(
            "/freeassociation.sourceforge.net/Tzfile/America/Argentina/Buenos_Aires"
        ),
        Some("America/Argentina/Buenos_Aires")
    );
    assert_eq!(
        strip_vendor_prefix("/America/New_York"),
        Some("America/New_York")
    );
    assert_eq!(
        strip_vendor_prefix("/America/Indiana/Indianapolis"),
        Some("America/Indiana/Indianapolis")
    );
    assert_eq!(strip_vendor_prefix("/UTC"), Some("UTC"));
    assert_eq!(strip_vendor_prefix("/US/Eastern"), Some("US/Eastern"));

    let empty_reg = TzRegistry::default();
    assert_eq!(
        empty_reg
            .resolve_canonical_iana("/America/New_York")
            .as_deref(),
        Some("America/New_York")
    );
    assert_eq!(
        empty_reg.resolve_canonical_iana("/UTC").as_deref(),
        Some("UTC")
    );
    assert_eq!(
        empty_reg.resolve_canonical_iana("/US/Eastern").as_deref(),
        Some("US/Eastern")
    );
    assert_eq!(
        empty_reg
            .resolve_canonical_iana("/Eastern Standard Time")
            .as_deref(),
        Some("America/New_York")
    );
    assert!(empty_reg.resolve("/America/New_York").is_some());

    // Standard ISO-style offsets
    assert_eq!(
        parse_fixed_offset_tzid("UTC+03:00").unwrap().seconds,
        10_800
    );
    assert_eq!(
        parse_fixed_offset_tzid("GMT-0530").unwrap().seconds,
        -(5 * 3600 + 1800)
    );
    // POSIX Etc/GMT sign inversion!
    assert_eq!(
        parse_fixed_offset_tzid("Etc/GMT+3").unwrap().seconds,
        -10_800
    );
    assert_eq!(
        parse_fixed_offset_tzid("Etc/GMT-3").unwrap().seconds,
        10_800
    );
}

#[test]
fn embedded_iana_dst_gap_and_fold_disambiguation() {
    let cal = Calendar::new();
    let reg = TzRegistry::from_calendar(&cal);

    let ny = reg.resolve("Eastern Standard Time").unwrap();

    // 2026-03-08 02:30:00 in America/New_York is in the spring-forward gap (02:00 -> 03:00).
    let gap_date = CalDate::new(2026, 3, 8).unwrap();
    let gap_time = CalTime::new(2, 30, 0).unwrap();

    let compat = ny
        .resolve_local(gap_date, gap_time, DstDisambiguation::Compatible)
        .unwrap();
    assert!(compat.was_gap);
    assert!(!compat.was_fold);
    assert_eq!(compat.offset_seconds, -4 * 3600);
    assert!(compat.is_dst);

    let earlier = ny
        .resolve_local(gap_date, gap_time, DstDisambiguation::Earlier)
        .unwrap();
    assert_eq!(earlier.offset_seconds, -5 * 3600);

    assert_eq!(
        ny.resolve_local(gap_date, gap_time, DstDisambiguation::Reject),
        Err(TzError::NonExistentLocalTime)
    );

    // 2026-11-01 01:30:00 in America/New_York is in the fall-back fold (02:00 -> 01:00).
    let fold_date = CalDate::new(2026, 11, 1).unwrap();
    let fold_time = CalTime::new(1, 30, 0).unwrap();

    let fold_compat = ny
        .resolve_local(fold_date, fold_time, DstDisambiguation::Compatible)
        .unwrap();
    assert!(fold_compat.was_fold);
    assert_eq!(fold_compat.offset_seconds, -4 * 3600); // earlier (EDT)

    let fold_later = ny
        .resolve_local(fold_date, fold_time, DstDisambiguation::Later)
        .unwrap();
    assert!(fold_later.was_fold);
    assert_eq!(fold_later.offset_seconds, -5 * 3600); // later (EST)
    assert_eq!(fold_later.utc_timestamp - fold_compat.utc_timestamp, 3600);

    assert_eq!(
        ny.resolve_local(fold_date, fold_time, DstDisambiguation::Reject),
        Err(TzError::AmbiguousLocalTime)
    );
}

#[test]
fn custom_vtimezone_rrule_and_rdate_compilation() {
    let ics = "\
BEGIN:VCALENDAR\r\n\
VERSION:2.0\r\n\
PRODID:-//icaloxide//vtz-test//EN\r\n\
BEGIN:VTIMEZONE\r\n\
TZID:X-CUSTOM-EUR\r\n\
BEGIN:DAYLIGHT\r\n\
TZOFFSETFROM:+0200\r\n\
TZOFFSETTO:+0300\r\n\
TZNAME:CEST-X\r\n\
DTSTART:19810329T020000\r\n\
RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU\r\n\
END:DAYLIGHT\r\n\
BEGIN:STANDARD\r\n\
TZOFFSETFROM:+0300\r\n\
TZOFFSETTO:+0200\r\n\
TZNAME:CET-X\r\n\
DTSTART:19961027T030000\r\n\
RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU\r\n\
END:STANDARD\r\n\
END:VTIMEZONE\r\n\
END:VCALENDAR\r\n";

    let cal = Calendar::parse_with_options(ics.as_bytes(), &ParseOptions::strict()).unwrap();
    let reg = cal.tz_registry();
    let custom = reg.resolve("X-CUSTOM-EUR").unwrap();

    // Winter 2026-01-15 -> +02:00
    let winter = custom
        .resolve_local(
            CalDate::new(2026, 1, 15).unwrap(),
            CalTime::new(12, 0, 0).unwrap(),
            DstDisambiguation::Compatible,
        )
        .unwrap();
    assert_eq!(winter.offset_seconds, 2 * 3600);
    assert!(!winter.is_dst);

    // Summer 2026-07-15 -> +03:00
    let summer = custom
        .resolve_local(
            CalDate::new(2026, 7, 15).unwrap(),
            CalTime::new(12, 0, 0).unwrap(),
            DstDisambiguation::Compatible,
        )
        .unwrap();
    assert_eq!(summer.offset_seconds, 3 * 3600);
    assert!(summer.is_dst);

    // Spring gap on 2026-03-29 02:30:00 (last Sunday of March 2026 is March 29)
    let gap = custom
        .resolve_local(
            CalDate::new(2026, 3, 29).unwrap(),
            CalTime::new(2, 30, 0).unwrap(),
            DstDisambiguation::Compatible,
        )
        .unwrap();
    assert!(gap.was_gap);
    assert_eq!(gap.offset_seconds, 3 * 3600);

    // Fall fold on 2026-10-25 02:30:00 (last Sunday of October 2026 is October 25)
    let fold_e = custom
        .resolve_local(
            CalDate::new(2026, 10, 25).unwrap(),
            CalTime::new(2, 30, 0).unwrap(),
            DstDisambiguation::Earlier,
        )
        .unwrap();
    let fold_l = custom
        .resolve_local(
            CalDate::new(2026, 10, 25).unwrap(),
            CalTime::new(2, 30, 0).unwrap(),
            DstDisambiguation::Later,
        )
        .unwrap();
    assert!(fold_e.was_fold && fold_l.was_fold);
    assert_eq!(fold_e.offset_seconds, 3 * 3600);
    assert_eq!(fold_l.offset_seconds, 2 * 3600);
    assert_eq!(fold_l.utc_timestamp - fold_e.utc_timestamp, 3600);
}

#[test]
fn add_missing_timezones_generates_accurate_vtimezones() {
    let ics = "\
BEGIN:VCALENDAR\r\n\
VERSION:2.0\r\n\
PRODID:-//icaloxide//tzgen//EN\r\n\
BEGIN:VEVENT\r\n\
UID:1\r\n\
DTSTART;TZID=Europe/Berlin:20260715T100000\r\n\
DTEND;TZID=America/New_York:20260715T120000\r\n\
END:VEVENT\r\n\
BEGIN:VEVENT\r\n\
UID:2\r\n\
DTSTART;TZID=Turkey Standard Time:20260115T090000\r\n\
DTSTAMP:20260115T060000Z\r\n\
END:VEVENT\r\n\
END:VCALENDAR\r\n";

    let mut cal = Calendar::parse_with_options(ics.as_bytes(), &ParseOptions::strict()).unwrap();
    assert_eq!(cal.timezones().count(), 0);

    let added = cal.add_missing_timezones();
    assert_eq!(added.len(), 3);
    assert_eq!(cal.timezones().count(), 3);

    // Re-serialize and strictly re-parse
    let serialized = cal.to_ical();
    let reparsed =
        Calendar::parse_with_options(serialized.as_bytes(), &ParseOptions::strict()).unwrap();
    assert_eq!(reparsed.timezones().count(), 3);

    // Calling again should be a no-op
    let mut reparsed_mut = reparsed;
    assert_eq!(reparsed_mut.add_missing_timezones().len(), 0);

    // Verify compiled VTIMEZONE for Europe/Berlin matches summer +02:00 & winter +01:00
    let reg = reparsed_mut.tz_registry();
    let berlin = reg.resolve("Europe/Berlin").unwrap();
    assert_eq!(
        berlin
            .resolve_local(
                CalDate::new(2026, 7, 15).unwrap(),
                CalTime::new(10, 0, 0).unwrap(),
                DstDisambiguation::Compatible,
            )
            .unwrap()
            .offset_seconds,
        2 * 3600
    );
    assert_eq!(
        berlin
            .resolve_local(
                CalDate::new(2026, 1, 15).unwrap(),
                CalTime::new(10, 0, 0).unwrap(),
                DstDisambiguation::Compatible,
            )
            .unwrap()
            .offset_seconds,
        3600
    );

    // Turkey Standard Time stays +03:00 year-round in 2026
    let tr = reg.resolve("Turkey Standard Time").unwrap();
    assert_eq!(
        tr.resolve_local(
            CalDate::new(2026, 1, 15).unwrap(),
            CalTime::new(9, 0, 0).unwrap(),
            DstDisambiguation::Compatible,
        )
        .unwrap()
        .offset_seconds,
        3 * 3600
    );
}
