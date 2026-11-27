use icaloxide_core::datetime::{
    CalDate, CalDateTime, CalDuration, CalPeriod, CalTime, TimeZoneRef, UtcOffset,
};
use icaloxide_core::error::{ParseErrorKind, ParseMode, ParseOptions};
use icaloxide_core::lexer::{
    escape_text, parse_content_line, split_escaped_list, unescape_text, UnfoldIter,
};
use icaloxide_core::{Calendar, ComponentKind, ParamName, PropertyName, PropertyValue};

#[test]
fn unfold_handles_bom_crlf_lf_tab_and_split_utf8() {
    // Split 2-byte UTF-8 'ğ' (0xC4 0x9F) deliberately across a CRLF+SPACE fold boundary!
    let mut raw = Vec::new();
    raw.extend_from_slice(b"\xEF\xBB\xBFBEGIN:VCALENDAR\r\n");
    raw.extend_from_slice(b"\n"); // empty line
    raw.extend_from_slice(b"SUMMARY:Merha");
    raw.push(0xC4);
    raw.extend_from_slice(b"\r\n ");
    raw.push(0x9F);
    raw.extend_from_slice(b"a\n\tD\xC3\xBCnya\rEND:VCALENDAR\r\n");

    let mut it = UnfoldIter::new(&raw, ParseMode::Strict, 4096);
    let mut warnings = Vec::new();

    let l1 = it.next_line(&mut warnings, true).unwrap().unwrap();
    assert_eq!(l1.line, 1);
    assert_eq!(l1.text.as_ref(), "BEGIN:VCALENDAR");

    let l2 = it.next_line(&mut warnings, true).unwrap().unwrap();
    assert_eq!(l2.line, 3);
    assert_eq!(l2.text.as_ref(), "SUMMARY:MerhağaDünya");

    let l3 = it.next_line(&mut warnings, true).unwrap().unwrap();
    assert_eq!(l3.text.as_ref(), "END:VCALENDAR");
    assert!(it.next_line(&mut warnings, true).is_none());
    assert_eq!(warnings, []);
}

#[test]
fn unfold_fast_path_borrows_without_allocation() {
    let raw = b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nEND:VCALENDAR\r\n";
    let mut it = UnfoldIter::new(raw, ParseMode::Strict, 4096);
    let mut warnings = Vec::new();
    let l1 = it.next_line(&mut warnings, true).unwrap().unwrap();
    assert!(matches!(l1.text, std::borrow::Cow::Borrowed(_)));
}

#[test]
fn content_line_quoted_params_and_rfc6868() {
    let line = r#"ATTENDEE;CN="Doe, John: ^'Boss^'^nCorp^^";ROLE=REQ-PARTICIPANT;MEMBER="mailto:a@x.com","mailto:b@y.com":mailto:john@example.com"#;
    let parsed = parse_content_line(1, line).unwrap();
    assert_eq!(parsed.name, "ATTENDEE");
    assert_eq!(parsed.value, "mailto:john@example.com");
    assert_eq!(parsed.params.len(), 3);
    assert_eq!(parsed.params[0].name, "CN");
    assert_eq!(
        parsed.params[0].values[0].as_ref(),
        "Doe, John: \"Boss\"\nCorp^"
    );
    assert_eq!(parsed.params[1].name, "ROLE");
    assert_eq!(parsed.params[1].values[0].as_ref(), "REQ-PARTICIPANT");
    assert_eq!(parsed.params[2].name, "MEMBER");
    assert_eq!(parsed.params[2].values.len(), 2);
    assert_eq!(parsed.params[2].values[0].as_ref(), "mailto:a@x.com");
    assert_eq!(parsed.params[2].values[1].as_ref(), "mailto:b@y.com");
}

#[test]
fn content_line_unterminated_quote_errors() {
    let line = r#"SUMMARY;LANGUAGE="en-US:Hello"#;
    let err = parse_content_line(7, line).unwrap_err();
    assert_eq!(err.line, 7);
    assert_eq!(err.kind, ParseErrorKind::UnterminatedQuote);
}

#[test]
fn text_escape_unescape_and_escaped_list() {
    let raw = r"Line1\nLine2\NBack\\Semi\;Comma\,End";
    let unescaped = unescape_text(raw);
    assert_eq!(unescaped.as_ref(), "Line1\nLine2\nBack\\Semi;Comma,End");

    let reescaped = escape_text(unescaped.as_ref());
    assert_eq!(reescaped.as_ref(), r"Line1\nLine2\nBack\\Semi\;Comma\,End");

    let cats = split_escaped_list(r"Work\, Urgent,Personal,Dev\;Ops");
    assert_eq!(cats, vec!["Work, Urgent", "Personal", "Dev;Ops"]);
}

#[test]
fn temporal_types_and_hinnant_calendar_roundtrip() {
    // Leap year rules
    assert!(CalDate::parse_bytes(b"20240229").is_ok());
    assert!(CalDate::parse_bytes(b"20000229").is_ok());
    assert!(CalDate::parse_bytes(b"20230229").is_err());
    assert!(CalDate::parse_bytes(b"19000229").is_err());

    // Epoch and weekday checks
    let epoch = CalDate::new(1970, 1, 1).unwrap();
    assert_eq!(epoch.to_days_since_epoch(), 0);
    assert_eq!(epoch.weekday(), 3); // Thursday

    let today = CalDate::new(2026, 11, 27).unwrap();
    assert_eq!(today.weekday(), 4); // Friday
    assert_eq!(
        CalDate::from_days_since_epoch(today.to_days_since_epoch()),
        today
    );

    // Full span roundtrip across 1900..=2100
    for year in 1900_i16..=2100 {
        for month in 1_u8..=12 {
            let d = CalDate::new(year, month, 1).unwrap();
            let round = CalDate::from_days_since_epoch(d.to_days_since_epoch());
            assert_eq!(round, d);
        }
    }

    // Time including leap second
    assert_eq!(
        CalTime::parse_bytes(b"235960").unwrap(),
        CalTime::new(23, 59, 60).unwrap()
    );
    assert!(CalTime::parse_bytes(b"240000").is_err());

    // DateTime UTC, floating, TZID, conflict
    let dt_utc = CalDateTime::parse_bytes(b"20260601T093000Z", None).unwrap();
    assert_eq!(dt_utc.tz, TimeZoneRef::Utc);
    assert_eq!(dt_utc.to_string(), "20260601T093000Z");

    let dt_tz = CalDateTime::parse_bytes(b"20260601T093000", Some("Europe/Istanbul")).unwrap();
    assert_eq!(dt_tz.tz, TimeZoneRef::TzId("Europe/Istanbul".into()));

    assert!(CalDateTime::parse_bytes(b"20260601T093000Z", Some("Europe/Istanbul")).is_err());

    // Duration
    let d1 = CalDuration::parse_bytes(b"-PT15M").unwrap();
    assert_eq!(d1.total_seconds(), -900);
    assert_eq!(d1.to_string(), "-PT15M");

    let d2 = CalDuration::parse_bytes(b"P2W").unwrap();
    assert_eq!(d2.total_seconds(), 14 * 86_400);
    assert_eq!(d2.to_string(), "P2W");

    let d3 = CalDuration::parse_bytes(b"P1DT2H3M4S").unwrap();
    assert_eq!(d3.total_seconds(), 86_400 + 7_200 + 180 + 4);

    // Period
    let p_dur = CalPeriod::parse_bytes(b"20260601T090000Z/PT1H", None).unwrap();
    assert_eq!(p_dur.to_string(), "20260601T090000Z/PT1H");

    // UTC Offset
    let off = UtcOffset::parse_bytes(b"+0300").unwrap();
    assert_eq!(off.seconds, 10_800);
    assert_eq!(off.to_string(), "+0300");
    let off_neg = UtcOffset::parse_bytes(b"-053015").unwrap();
    assert_eq!(off_neg.seconds, -(5 * 3600 + 30 * 60 + 15));
    assert_eq!(off_neg.to_string(), "-053015");
}

#[test]
fn full_calendar_rfc_extensions_and_roundtrip() {
    let ics = "\
BEGIN:VCALENDAR\r\n\
VERSION:2.0\r\n\
PRODID:-//icaloxide//test//EN\r\n\
NAME:Takvimim\r\n\
COLOR:crimson\r\n\
REFRESH-INTERVAL;VALUE=DURATION:PT1H\r\n\
X-WR-TIMEZONE:Europe/Istanbul\r\n\
BEGIN:VEVENT\r\n\
UID:evt-1@icaloxide\r\n\
DTSTAMP:20261127T000000Z\r\n\
DTSTART;TZID=Europe/Istanbul:20261127T103000\r\n\
DURATION:PT45M\r\n\
SUMMARY:Toplantı — ğüşıöçĞÜŞİÖÇ 🗓️ çok uzun başlık katlama testi için ekstra kelimeler ekliyoruz\r\n\
CATEGORIES:İş\\, Öncelik,Mühendislik\r\n\
GEO:41.0082;28.9784\r\n\
CONFERENCE;VALUE=URI;FEATURE=VIDEO:https://meet.example.com/123\r\n\
EXDATE;VALUE=DATE:20261204,20261211\r\n\
RRULE:FREQ=WEEKLY;COUNT=10\r\n\
BEGIN:VALARM\r\n\
ACTION:DISPLAY\r\n\
DESCRIPTION:Hatırlatma\r\n\
TRIGGER:-PT15M\r\n\
END:VALARM\r\n\
BEGIN:VLOCATION\r\n\
UID:loc-1\r\n\
NAME:Ana Ofis\r\n\
LOCATION-TYPE:OFFICE\r\n\
END:VLOCATION\r\n\
END:VEVENT\r\n\
BEGIN:VAVAILABILITY\r\n\
UID:avail-1\r\n\
BUSYTYPE:BUSY-UNAVAILABLE\r\n\
BEGIN:AVAILABLE\r\n\
UID:av-1\r\n\
DTSTART:20261127T090000Z\r\n\
DTEND:20261127T180000Z\r\n\
END:AVAILABLE\r\n\
END:VAVAILABILITY\r\n\
END:VCALENDAR\r\n";

    let cal = Calendar::parse_with_options(ics.as_bytes(), &ParseOptions::strict()).unwrap();
    assert_eq!(cal.name(), Some("Takvimim"));
    assert_eq!(cal.events().count(), 1);
    assert_eq!(cal.availabilities().count(), 1);

    let ev = cal.events().next().unwrap();
    assert_eq!(ev.uid(), Some("evt-1@icaloxide"));
    assert_eq!(ev.children().count(), 2);

    let cats = ev.property(&PropertyName::Categories).unwrap();
    assert_eq!(
        cats.value,
        PropertyValue::TextList(vec!["İş, Öncelik".into(), "Mühendislik".into()])
    );

    let geo = ev.property(&PropertyName::Geo).unwrap();
    assert_eq!(
        geo.value,
        PropertyValue::Geo {
            lat: 41.0082,
            lon: 28.9784
        }
    );

    let dtstart = ev.property(&PropertyName::DtStart).unwrap();
    assert_eq!(
        dtstart.param_first(&ParamName::TzId),
        Some("Europe/Istanbul")
    );

    let alarm = ev
        .children()
        .find(|c| c.kind() == &ComponentKind::VAlarm)
        .unwrap();
    assert_eq!(
        alarm.property(&PropertyName::Trigger).unwrap().value,
        PropertyValue::Duration(CalDuration::parse_bytes(b"-PT15M").unwrap())
    );

    // Serialize and verify RFC 5545 75-octet UTF-8-safe folding + lossless roundtrip
    let serialized = cal.to_ical();
    for physical_line in serialized.split("\r\n").filter(|l| !l.is_empty()) {
        assert!(
            physical_line.len() <= 75,
            "line exceeded 75 octets ({} bytes): {:?}",
            physical_line.len(),
            physical_line
        );
    }

    let reparsed =
        Calendar::parse_with_options(serialized.as_bytes(), &ParseOptions::strict()).unwrap();
    let ev2 = reparsed.events().next().unwrap();
    assert_eq!(ev2.summary(), ev.summary());
    assert_eq!(
        ev2.property(&PropertyName::Categories).unwrap().value,
        cats.value
    );
    assert_eq!(
        ev2.property(&PropertyName::ExDate).unwrap().value,
        ev.property(&PropertyName::ExDate).unwrap().value
    );
}

#[test]
fn strict_vs_lenient_recovery_and_dos_guards() {
    let broken = "\
X-OUTSIDE:ignored\r\n\
BEGIN:VCALENDAR\r\n\
VERSION:2.0\r\n\
BADLINEWITHOUTCOLON\r\n\
BEGIN:VEVENT\r\n\
DTSTART;TZID=Europe/Istanbul:20260601T100000Z\r\n\
DTEND:NOT-A-DATE\r\n\
SUMMARY:Ok\r\n\
END:VCALENDAR\r\n";

    // Strict rejects property outside component
    assert!(Calendar::parse_with_options(broken.as_bytes(), &ParseOptions::strict()).is_err());

    // Lenient recovers and records warnings
    let cal = Calendar::parse_with_options(broken.as_bytes(), &ParseOptions::lenient()).unwrap();
    assert!(cal.warnings().len() >= 4);
    let ev = cal.events().next().unwrap();
    assert_eq!(ev.summary(), Some("Ok"));
    assert!(matches!(
        ev.property(&PropertyName::DtEnd).unwrap().value,
        PropertyValue::Raw(_)
    ));

    // DoS depth guard
    let mut opts = ParseOptions::strict();
    opts.max_depth = 2;
    let deep = "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nBEGIN:VALARM\r\nEND:VALARM\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n";
    let err = Calendar::parse_with_options(deep.as_bytes(), &opts).unwrap_err();
    assert_eq!(err.kind, ParseErrorKind::MaxDepthExceeded { max_depth: 2 });
}

#[test]
fn used_missing_tzids_uid_lookup_and_validate() {
    let ics = "\
BEGIN:VCALENDAR\r\n\
VERSION:2.0\r\n\
PRODID:-//icaloxide//val//EN\r\n\
BEGIN:VEVENT\r\n\
UID:u-1\r\n\
DTSTART;TZID=Europe/Istanbul:20260601T100000\r\n\
END:VEVENT\r\n\
END:VCALENDAR\r\n";

    let cal = Calendar::parse_str(ics).unwrap();
    assert_eq!(cal.used_tzids(), vec!["Europe/Istanbul"]);
    assert_eq!(cal.missing_tzids(), vec!["Europe/Istanbul"]);
    assert_eq!(cal.components_with_uid("u-1").len(), 1);
    let issues = cal.validate();
    assert!(issues.iter().any(|w| w.message.contains("DTSTAMP")));
}

#[test]
fn upstream_issue_regressions_charset_request_status_period_tzid_standalone() {
    // #2078: Legacy CHARSET=ISO-8859-1 byte decoding without U+FFFD replacement
    let mut latin1_ics = Vec::new();
    latin1_ics.extend_from_slice(
        b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\nBEGIN:VEVENT\r\nSUMMARY;CHARSET=ISO-8859-1:Caf",
    );
    latin1_ics.push(0xE9); // Latin-1 'é'
    latin1_ics.extend_from_slice(
        b"\r\nREQUEST-STATUS:2.0;Success;Extra\\;Info\r\nDTSTART;TZID=UTC:20260601T100000Z\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n",
    );
    let cal = Calendar::parse_with_options(&latin1_ics, &ParseOptions::lenient()).unwrap();
    let ev = cal.events().next().unwrap();
    assert_eq!(ev.summary(), Some("Café"));

    // #2123: REQUEST-STATUS preserves unescaped ';' field separators on round-trip
    let serialized = cal.to_ical();
    assert!(serialized.contains("REQUEST-STATUS:2.0;Success;Extra\\;Info"));
    // #2020: TZID=UTC must NOT be serialized alongside trailing Z
    assert!(serialized.contains("DTSTART:20260601T100000Z"));
    assert!(!serialized.contains("DTSTART;TZID=UTC:20260601T100000Z"));

    // #2087: Zero-length PERIOD rejected in Strict mode, tolerated with warning in Lenient
    let zero_period_ics = b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\nBEGIN:VFREEBUSY\r\nFREEBUSY:20260101T100000Z/PT0S\r\nEND:VFREEBUSY\r\nEND:VCALENDAR\r\n";
    assert!(Calendar::parse_with_options(zero_period_ics, &ParseOptions::strict()).is_err());
    let len_fb = Calendar::parse_with_options(zero_period_ics, &ParseOptions::lenient()).unwrap();
    assert_ne!(len_fb.warnings(), []);

    // Standalone component parsing without BEGIN:VCALENDAR
    let ev_only = b"BEGIN:VEVENT\r\nUID:solo\r\nDTSTART:20260601T100000Z\r\nSUMMARY:Standalone\r\nEND:VEVENT\r\n";
    let solo = Calendar::parse_component(ev_only, &ParseOptions::strict()).unwrap();
    assert_eq!(solo.root().kind(), &ComponentKind::VEvent);
    assert_eq!(solo.root().summary(), Some("Standalone"));

    // #2111 & #2075: duplicate TZID inside VTIMEZONE & custom VTIMEZONE overrides global TZDB
    let dup_vtz = b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\nBEGIN:VTIMEZONE\r\nTZID:America/New_York\r\nTZID:America/New_York\r\nBEGIN:STANDARD\r\nDTSTART:19700101T000000\r\nTZOFFSETFROM:+0900\r\nTZOFFSETTO:+0900\r\nEND:STANDARD\r\nEND:VTIMEZONE\r\nEND:VCALENDAR\r\n";
    let dup_cal = Calendar::parse_with_options(dup_vtz, &ParseOptions::strict()).unwrap();
    let reg = dup_cal.tz_registry();
    let resolved = reg.resolve("America/New_York").unwrap();
    let r = resolved
        .resolve_local(
            icaloxide_core::CalDate::new(2026, 6, 1).unwrap(),
            icaloxide_core::CalTime::new(12, 0, 0).unwrap(),
            icaloxide_core::DstDisambiguation::Compatible,
        )
        .unwrap();
    assert_eq!(r.offset_seconds, 9 * 3600);

    // build_vtimezone_for_tzid works cleanly
    let built =
        icaloxide_core::tz::build_vtimezone_for_tzid("America/New_York", 2020, 2030).unwrap();
    assert_eq!(built.root().kind(), &ComponentKind::VTimezone);
}
