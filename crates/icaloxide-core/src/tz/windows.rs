use crate::datetime::UtcOffset;

const IANA_ALIASES: &[(&str, &str)] = &[
    // UTC / GMT aliases
    ("UTC", "UTC"),
    ("ETC/UTC", "UTC"),
    ("ETC/UCT", "UTC"),
    ("UCT", "UTC"),
    ("GMT", "UTC"),
    ("ETC/GMT", "UTC"),
    ("ETC/GREENWICH", "UTC"),
    ("Z", "UTC"),
    ("ZULU", "UTC"),
    ("COORDINATED UNIVERSAL TIME", "UTC"),
    ("UTC+00:00", "UTC"),
    ("GMT+00:00", "UTC"),
    // Legacy US / IANA aliases
    ("US/EASTERN", "America/New_York"),
    ("EST5EDT", "America/New_York"),
    ("US/CENTRAL", "America/Chicago"),
    ("CST6CDT", "America/Chicago"),
    ("US/MOUNTAIN", "America/Denver"),
    ("MST7MDT", "America/Denver"),
    ("US/PACIFIC", "America/Los_Angeles"),
    ("PST8PDT", "America/Los_Angeles"),
    ("US/ALASKA", "America/Anchorage"),
    ("US/HAWAII", "Pacific/Honolulu"),
    ("US/ARIZONA", "America/Phoenix"),
    ("CANADA/EASTERN", "America/Toronto"),
    ("CANADA/PACIFIC", "America/Vancouver"),
    ("EUROPE/KIEV", "Europe/Kyiv"),
    ("ASIA/CALCUTTA", "Asia/Kolkata"),
    ("ASIA/SAIGON", "Asia/Ho_Chi_Minh"),
    ("ASIA/KATMANDU", "Asia/Kathmandu"),
    // Windows CLDR zones — Europe, Middle East & Africa
    ("TURKEY STANDARD TIME", "Europe/Istanbul"),
    ("W. EUROPE STANDARD TIME", "Europe/Berlin"),
    ("ROMANCE STANDARD TIME", "Europe/Paris"),
    ("CENTRAL EUROPE STANDARD TIME", "Europe/Budapest"),
    ("CENTRAL EUROPEAN STANDARD TIME", "Europe/Warsaw"),
    ("GMT STANDARD TIME", "Europe/London"),
    ("GREENWICH STANDARD TIME", "Atlantic/Reykjavik"),
    ("E. EUROPE STANDARD TIME", "Europe/Chisinau"),
    ("FLE STANDARD TIME", "Europe/Kyiv"),
    ("GTB STANDARD TIME", "Europe/Bucharest"),
    ("RUSSIAN STANDARD TIME", "Europe/Moscow"),
    ("BELARUS STANDARD TIME", "Europe/Minsk"),
    ("KALININGRAD STANDARD TIME", "Europe/Kaliningrad"),
    ("RUSSIA TIME ZONE 3", "Europe/Samara"),
    ("EKATERINBURG STANDARD TIME", "Asia/Yekaterinburg"),
    ("AZORES STANDARD TIME", "Atlantic/Azores"),
    ("CAPE VERDE STANDARD TIME", "Atlantic/Cape_Verde"),
    ("MOROCCO STANDARD TIME", "Africa/Casablanca"),
    ("W. CENTRAL AFRICA STANDARD TIME", "Africa/Lagos"),
    ("EGYPT STANDARD TIME", "Africa/Cairo"),
    ("SOUTH AFRICA STANDARD TIME", "Africa/Johannesburg"),
    ("E. AFRICA STANDARD TIME", "Africa/Nairobi"),
    ("LIBYA STANDARD TIME", "Africa/Tripoli"),
    ("NAMIBIA STANDARD TIME", "Africa/Windhoek"),
    ("ISRAEL STANDARD TIME", "Asia/Jerusalem"),
    ("JORDAN STANDARD TIME", "Asia/Amman"),
    ("MIDDLE EAST STANDARD TIME", "Asia/Beirut"),
    ("SYRIA STANDARD TIME", "Asia/Damascus"),
    ("ARABIC STANDARD TIME", "Asia/Baghdad"),
    ("ARAB STANDARD TIME", "Asia/Riyadh"),
    ("ARABIAN STANDARD TIME", "Asia/Dubai"),
    ("IRAN STANDARD TIME", "Asia/Tehran"),
    ("CAUCASUS STANDARD TIME", "Asia/Yerevan"),
    ("AZERBAIJAN STANDARD TIME", "Asia/Baku"),
    ("GEORGIAN STANDARD TIME", "Asia/Tbilisi"),
    ("AFGHANISTAN STANDARD TIME", "Asia/Kabul"),
    // Windows CLDR zones — Asia & Oceania
    ("WEST ASIA STANDARD TIME", "Asia/Tashkent"),
    ("PAKISTAN STANDARD TIME", "Asia/Karachi"),
    ("INDIA STANDARD TIME", "Asia/Kolkata"),
    ("SRI LANKA STANDARD TIME", "Asia/Colombo"),
    ("NEPAL STANDARD TIME", "Asia/Kathmandu"),
    ("CENTRAL ASIA STANDARD TIME", "Asia/Almaty"),
    ("BANGLADESH STANDARD TIME", "Asia/Dhaka"),
    ("MYANMAR STANDARD TIME", "Asia/Yangon"),
    ("SE ASIA STANDARD TIME", "Asia/Bangkok"),
    ("N. CENTRAL ASIA STANDARD TIME", "Asia/Novosibirsk"),
    ("NORTH ASIA STANDARD TIME", "Asia/Krasnoyarsk"),
    ("NORTH ASIA EAST STANDARD TIME", "Asia/Irkutsk"),
    ("CHINA STANDARD TIME", "Asia/Shanghai"),
    ("SINGAPORE STANDARD TIME", "Asia/Singapore"),
    ("W. AUSTRALIA STANDARD TIME", "Australia/Perth"),
    ("TAIPEI STANDARD TIME", "Asia/Taipei"),
    ("ULAANBAATAR STANDARD TIME", "Asia/Ulaanbaatar"),
    ("TOKYO STANDARD TIME", "Asia/Tokyo"),
    ("KOREA STANDARD TIME", "Asia/Seoul"),
    ("YAKUTSK STANDARD TIME", "Asia/Yakutsk"),
    ("CEN. AUSTRALIA STANDARD TIME", "Australia/Adelaide"),
    ("AUS CENTRAL STANDARD TIME", "Australia/Darwin"),
    ("E. AUSTRALIA STANDARD TIME", "Australia/Brisbane"),
    ("AUS EASTERN STANDARD TIME", "Australia/Sydney"),
    ("TASMANIA STANDARD TIME", "Australia/Hobart"),
    ("WEST PACIFIC STANDARD TIME", "Pacific/Port_Moresby"),
    ("VLADIVOSTOK STANDARD TIME", "Asia/Vladivostok"),
    ("CENTRAL PACIFIC STANDARD TIME", "Pacific/Guadalcanal"),
    ("NEW ZEALAND STANDARD TIME", "Pacific/Auckland"),
    ("FIJI STANDARD TIME", "Pacific/Fiji"),
    ("TONGA STANDARD TIME", "Pacific/Tongatapu"),
    ("SAMOA STANDARD TIME", "Pacific/Apia"),
    // Windows CLDR zones — Americas
    ("NEWFOUNDLAND STANDARD TIME", "America/St_Johns"),
    ("ATLANTIC STANDARD TIME", "America/Halifax"),
    ("E. SOUTH AMERICA STANDARD TIME", "America/Sao_Paulo"),
    ("ARGENTINA STANDARD TIME", "America/Buenos_Aires"),
    ("SA EASTERN STANDARD TIME", "America/Cayenne"),
    ("GREENLAND STANDARD TIME", "America/Godthab"),
    ("MONTEVIDEO STANDARD TIME", "America/Montevideo"),
    ("PARAGUAY STANDARD TIME", "America/Asuncion"),
    ("PACIFIC SA STANDARD TIME", "America/Santiago"),
    ("SA WESTERN STANDARD TIME", "America/La_Paz"),
    ("VENEZUELA STANDARD TIME", "America/Caracas"),
    ("SA PACIFIC STANDARD TIME", "America/Bogota"),
    ("EASTERN STANDARD TIME", "America/New_York"),
    ("US EASTERN STANDARD TIME", "America/Indianapolis"),
    ("CUBA STANDARD TIME", "America/Havana"),
    ("HAITI STANDARD TIME", "America/Port-au-Prince"),
    ("CENTRAL STANDARD TIME", "America/Chicago"),
    ("CENTRAL AMERICA STANDARD TIME", "America/Guatemala"),
    ("CANADA CENTRAL STANDARD TIME", "America/Regina"),
    ("CENTRAL STANDARD TIME (MEXICO)", "America/Mexico_City"),
    ("MOUNTAIN STANDARD TIME", "America/Denver"),
    ("US MOUNTAIN STANDARD TIME", "America/Phoenix"),
    ("MOUNTAIN STANDARD TIME (MEXICO)", "America/Mazatlan"),
    ("PACIFIC STANDARD TIME", "America/Los_Angeles"),
    ("PACIFIC STANDARD TIME (MEXICO)", "America/Tijuana"),
    ("ALASKAN STANDARD TIME", "America/Anchorage"),
    ("HAWAIIAN STANDARD TIME", "Pacific/Honolulu"),
    ("DATELINE STANDARD TIME", "Etc/GMT+12"),
    ("UTC-11", "Etc/GMT+11"),
    ("UTC-02", "Etc/GMT+2"),
    ("UTC+12", "Etc/GMT-12"),
    ("UTC+13", "Etc/GMT-13"),
];

/// Resolve a Windows / Outlook / legacy timezone identifier to a canonical IANA TZID.
///
/// Matching is ASCII case-insensitive after trimming surrounding whitespace and quotes.
pub fn resolve_iana_alias(raw: &str) -> Option<&'static str> {
    let cleaned = raw.trim().trim_matches('"').trim();
    if cleaned.is_empty() {
        return None;
    }

    // Strip optional "(UTC+XX:XX) " prefix sometimes emitted by older Outlook exports.
    let normalized = if cleaned.starts_with('(') {
        cleaned.split_once(')').map_or(cleaned, |(_, r)| r.trim())
    } else {
        cleaned
    };

    let upper = normalized.to_ascii_uppercase();
    IANA_ALIASES
        .iter()
        .find(|(key, _)| *key == upper)
        .map(|(_, val)| *val)
}

/// Strip vendor TZID prefixes such as `/mozilla.org/20050126_1/Europe/Berlin`
/// or `/freeassociation.sourceforge.net/Tzfile/America/New_York`.
pub fn strip_vendor_prefix(tzid: &str) -> Option<&str> {
    let trimmed = tzid.trim().trim_matches('"');
    if !trimmed.starts_with('/') {
        return None;
    }
    let parts: Vec<&str> = trimmed.split('/').filter(|s| !s.is_empty()).collect();
    if parts.len() >= 3 {
        if parts.len() >= 4 {
            if let Some(cand) = suffix_after_nth_last_slash(trimmed, 3) {
                if jiff::tz::TimeZone::get(cand).is_ok() {
                    return Some(cand);
                }
            }
        }
        if let Some(two_seg) = suffix_after_nth_last_slash(trimmed, 2) {
            return Some(two_seg);
        }
    }
    None
}

fn suffix_after_nth_last_slash(s: &str, segments: usize) -> Option<&str> {
    let mut count = 0;
    for (idx, _) in s.rmatch_indices('/') {
        count += 1;
        if count == segments {
            let candidate = &s[idx + 1..];
            if !candidate.is_empty() {
                return Some(candidate);
            }
        }
    }
    None
}

/// Parse fixed-offset TZIDs such as `UTC+03:00`, `UTC-0500`, `GMT+02:00`, `+03:00`,
/// or POSIX-inverted `Etc/GMT+3` (`-03:00`) / `Etc/GMT-3` (`+03:00`).
pub fn parse_fixed_offset_tzid(tzid: &str) -> Option<UtcOffset> {
    let cleaned = tzid.trim().trim_matches('"').trim();
    if cleaned.is_empty() {
        return None;
    }

    // Handle POSIX Etc/GMT[+/-]N (sign is inverted relative to ISO-8601!).
    if let Some(rest) = strip_prefix_ignore_ascii_case(cleaned, "Etc/GMT") {
        if rest.is_empty() {
            return Some(UtcOffset { seconds: 0 });
        }
        let (posix_sign, digits) = if let Some(d) = rest.strip_prefix('+') {
            (-1_i32, d)
        } else {
            let d = rest.strip_prefix('-')?;
            (1_i32, d)
        };
        let hours = digits.parse::<i32>().ok()?;
        return (0..=14).contains(&hours).then_some(UtcOffset {
            seconds: posix_sign * hours * 3600,
        });
    }

    let rest = if let Some(r) = strip_prefix_ignore_ascii_case(cleaned, "UTC") {
        r.trim()
    } else if let Some(r) = strip_prefix_ignore_ascii_case(cleaned, "GMT") {
        r.trim()
    } else {
        cleaned
    };

    if rest.is_empty()
        && (cleaned.eq_ignore_ascii_case("UTC") || cleaned.eq_ignore_ascii_case("GMT"))
    {
        return Some(UtcOffset { seconds: 0 });
    }

    let (sign, body) = if let Some(b) = rest.strip_prefix('+') {
        (1_i32, b)
    } else {
        let b = rest.strip_prefix('-')?;
        (-1_i32, b)
    };

    let (hours, minutes) = if let Some((h_s, m_s)) = body.split_once(':') {
        (h_s.parse::<i32>().ok()?, m_s.parse::<i32>().ok()?)
    } else if body.len() == 4 && body.bytes().all(|b| b.is_ascii_digit()) {
        (
            body[0..2].parse::<i32>().ok()?,
            body[2..4].parse::<i32>().ok()?,
        )
    } else if (1..=2).contains(&body.len()) && body.bytes().all(|b| b.is_ascii_digit()) {
        (body.parse::<i32>().ok()?, 0)
    } else {
        return None;
    };

    if !(0..=23).contains(&hours) || !(0..=59).contains(&minutes) {
        return None;
    }

    Some(UtcOffset {
        seconds: sign * (hours * 3600 + minutes * 60),
    })
}

fn strip_prefix_ignore_ascii_case<'a>(s: &'a str, prefix: &str) -> Option<&'a str> {
    let plen = prefix.len();
    if s.len() >= plen && s.is_char_boundary(plen) && s[..plen].eq_ignore_ascii_case(prefix) {
        Some(&s[plen..])
    } else {
        None
    }
}
