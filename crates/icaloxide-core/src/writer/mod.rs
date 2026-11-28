use std::fmt::{self, Write};

use crate::datetime::TimeZoneRef;
use crate::lexer::escape_text;
use crate::model::{Calendar, ComponentArena, ComponentId, ParamName, Property, PropertyValue};

/// RFC 5545 Section 3.1 maximum octets per line (excluding `\r\n`).
const MAX_LINE_OCTETS: usize = 75;

/// Serialize an entire [`Calendar`] into `out` using `\r\n` endings and UTF-8-safe 75-octet folding.
pub fn write_calendar(cal: &Calendar, out: &mut impl Write) -> fmt::Result {
    write_component_ordered(&cal.arena, cal.root_id(), false, out)
}

/// Serialize an entire [`Calendar`] with deterministically sorted properties per component.
pub fn write_calendar_sorted(cal: &Calendar, out: &mut impl Write) -> fmt::Result {
    write_component_ordered(&cal.arena, cal.root_id(), true, out)
}

/// Serialize a single component (and its nested subcomponents) into `out`.
pub fn write_component(
    arena: &ComponentArena,
    id: ComponentId,
    out: &mut impl Write,
) -> fmt::Result {
    write_component_ordered(arena, id, false, out)
}

/// Serialize a single component (and nested subcomponents) into `out`, optionally sorting properties by name.
pub fn write_component_ordered(
    arena: &ComponentArena,
    id: ComponentId,
    sorted: bool,
    out: &mut impl Write,
) -> fmt::Result {
    let Some(node) = arena.get(id) else {
        return Ok(());
    };

    let mut line_buf = String::with_capacity(128);

    // BEGIN:<COMPONENT>
    line_buf.push_str("BEGIN:");
    line_buf.push_str(node.kind.as_str());
    write_folded_line(&line_buf, out)?;

    // Properties
    if sorted && node.properties.len() > 1 {
        let mut refs: Vec<&Property> = node.properties.iter().collect();
        refs.sort_by(|a, b| a.name.as_str().cmp(b.name.as_str()));
        for prop in refs {
            line_buf.clear();
            format_property(prop, &mut line_buf)?;
            write_folded_line(&line_buf, out)?;
        }
    } else {
        for prop in &node.properties {
            line_buf.clear();
            format_property(prop, &mut line_buf)?;
            write_folded_line(&line_buf, out)?;
        }
    }

    // Nested child components
    for &child_id in &node.children {
        write_component_ordered(arena, child_id, sorted, out)?;
    }

    // END:<COMPONENT>
    line_buf.clear();
    line_buf.push_str("END:");
    line_buf.push_str(node.kind.as_str());
    write_folded_line(&line_buf, out)?;

    Ok(())
}

fn format_property(prop: &Property, buf: &mut String) -> fmt::Result {
    buf.push_str(prop.name.as_str());

    let is_utc_dt = match &prop.value {
        PropertyValue::DateTime(dt) => matches!(dt.tz, TimeZoneRef::Utc),
        PropertyValue::DateTimeList(list) => {
            !list.is_empty() && list.iter().all(|dt| matches!(dt.tz, TimeZoneRef::Utc))
        }
        _ => false,
    };
    let has_tzid_param = prop.params.iter().any(|p| p.name == ParamName::TzId);

    for param in &prop.params {
        if is_utc_dt && param.name == ParamName::TzId {
            continue;
        }
        buf.push(';');
        buf.push_str(param.name.as_str());
        buf.push('=');
        for (idx, val) in param.values.iter().enumerate() {
            if idx > 0 {
                buf.push(',');
            }
            write_param_value(val.as_str(), buf);
        }
    }

    // Ensure TZID carried on CalDateTime values is preserved if not already in params.
    if !has_tzid_param && !is_utc_dt {
        if let Some(tzid) = extract_tzid(&prop.value) {
            buf.push(';');
            buf.push_str("TZID=");
            write_param_value(tzid, buf);
        }
    }

    buf.push(':');

    match &prop.value {
        PropertyValue::Text(s) => buf.push_str(escape_text(s.as_str()).as_ref()),
        PropertyValue::TextList(items) => {
            for (idx, item) in items.iter().enumerate() {
                if idx > 0 {
                    buf.push(',');
                }
                buf.push_str(escape_text(item.as_str()).as_ref());
            }
        }
        PropertyValue::Date(d) => write!(buf, "{d}")?,
        PropertyValue::DateTime(dt) => write!(buf, "{dt}")?,
        PropertyValue::DateList(items) => {
            for (idx, item) in items.iter().enumerate() {
                if idx > 0 {
                    buf.push(',');
                }
                write!(buf, "{item}")?;
            }
        }
        PropertyValue::DateTimeList(items) => {
            for (idx, item) in items.iter().enumerate() {
                if idx > 0 {
                    buf.push(',');
                }
                write!(buf, "{item}")?;
            }
        }
        PropertyValue::Duration(dur) => write!(buf, "{dur}")?,
        PropertyValue::Period(p) => write!(buf, "{p}")?,
        PropertyValue::PeriodList(items) => {
            for (idx, item) in items.iter().enumerate() {
                if idx > 0 {
                    buf.push(',');
                }
                write!(buf, "{item}")?;
            }
        }
        PropertyValue::Integer(v) => write!(buf, "{v}")?,
        PropertyValue::Float(v) => write!(buf, "{v}")?,
        PropertyValue::Geo { lat, lon } => write!(buf, "{lat};{lon}")?,
        PropertyValue::Boolean(b) => buf.push_str(if *b { "TRUE" } else { "FALSE" }),
        PropertyValue::UtcOffset(off) => write!(buf, "{off}")?,
        PropertyValue::Time { time, is_utc } => {
            write!(buf, "{time}")?;
            if *is_utc {
                buf.push('Z');
            }
        }
        PropertyValue::RequestStatus(parts) => {
            for (idx, part) in parts.iter().enumerate() {
                if idx > 0 {
                    buf.push(';');
                }
                buf.push_str(escape_text(part.as_str()).as_ref());
            }
        }
        PropertyValue::Uri(s)
        | PropertyValue::CalAddress(s)
        | PropertyValue::Binary(s)
        | PropertyValue::Recur(s)
        | PropertyValue::Raw(s) => buf.push_str(s.as_str()),
    }

    Ok(())
}

fn extract_tzid(value: &PropertyValue) -> Option<&str> {
    match value {
        PropertyValue::DateTime(dt) => match &dt.tz {
            TimeZoneRef::TzId(id) => Some(id.as_str()),
            _ => None,
        },
        PropertyValue::DateTimeList(list) => list.first().and_then(|dt| match &dt.tz {
            TimeZoneRef::TzId(id) => Some(id.as_str()),
            _ => None,
        }),
        PropertyValue::Period(p) => match p {
            crate::datetime::CalPeriod::Explicit { start, .. }
            | crate::datetime::CalPeriod::Duration { start, .. } => match &start.tz {
                TimeZoneRef::TzId(id) => Some(id.as_str()),
                _ => None,
            },
        },
        _ => None,
    }
}

/// Write a parameter value with RFC 6868 encoding and double-quoting when required.
fn write_param_value(raw: &str, buf: &mut String) {
    let needs_quotes = raw
        .bytes()
        .any(|b| matches!(b, b':' | b';' | b',' | b'"' | b'\n' | b'^'));

    if !needs_quotes {
        buf.push_str(raw);
        return;
    }

    buf.push('"');
    for ch in raw.chars() {
        match ch {
            '^' => buf.push_str("^^"),
            '\n' => buf.push_str("^n"),
            '"' => buf.push_str("^'"),
            other => buf.push(other),
        }
    }
    buf.push('"');
}

/// Fold a single logical line at <= 75 UTF-8 octets on valid character boundaries,
/// emitting `\r\n` between segments and `\r\n` at the end.
pub fn write_folded_line(line: &str, out: &mut impl Write) -> fmt::Result {
    if line.len() <= MAX_LINE_OCTETS {
        out.write_str(line)?;
        return out.write_str("\r\n");
    }

    let mut start = 0;
    let mut is_first_segment = true;

    while start < line.len() {
        // Continuation lines start with 1 space (0x20), leaving 74 content octets.
        let budget = if is_first_segment {
            MAX_LINE_OCTETS
        } else {
            MAX_LINE_OCTETS - 1
        };

        let remaining = &line[start..];
        let mut chunk_bytes = 0;
        for ch in remaining.chars() {
            let clen = ch.len_utf8();
            if chunk_bytes > 0 && chunk_bytes + clen > budget {
                break;
            }
            chunk_bytes += clen;
        }

        if !is_first_segment {
            out.write_str("\r\n ")?;
        }
        out.write_str(&line[start..start + chunk_bytes])?;
        start += chunk_bytes;
        is_first_segment = false;
    }

    out.write_str("\r\n")
}
