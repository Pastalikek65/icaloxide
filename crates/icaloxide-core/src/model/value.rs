use compact_str::CompactString;

use crate::datetime::{CalDate, CalDateTime, CalDuration, CalPeriod, CalTime, UtcOffset};
use crate::error::{DateTimeError, ParseError, ParseErrorKind, ParseMode, ParseWarning};
use crate::lexer::{split_escaped_list, split_escaped_semicolon_list, unescape_text};
use crate::model::known::{PropertyName, ValueType};

/// Typed RFC 5545 property value stored in the arena.
#[derive(Debug, Clone, PartialEq)]
pub enum PropertyValue {
    Text(CompactString),
    TextList(Vec<CompactString>),
    Date(CalDate),
    DateTime(CalDateTime),
    DateList(Vec<CalDate>),
    DateTimeList(Vec<CalDateTime>),
    Duration(CalDuration),
    Period(CalPeriod),
    PeriodList(Vec<CalPeriod>),
    Integer(i64),
    Float(f64),
    Geo { lat: f64, lon: f64 },
    Boolean(bool),
    UtcOffset(UtcOffset),
    Time(CalTime),
    RequestStatus(Vec<CompactString>),
    Uri(CompactString),
    CalAddress(CompactString),
    Binary(CompactString),
    Recur(CompactString),
    Raw(CompactString),
}

impl PropertyValue {
    /// Return string content if this value is text-like (`Text`, `Uri`, `CalAddress`, `Recur`, `Raw`, `Binary`).
    pub fn as_str(&self) -> Option<&str> {
        match self {
            Self::Text(s)
            | Self::Uri(s)
            | Self::CalAddress(s)
            | Self::Recur(s)
            | Self::Raw(s)
            | Self::Binary(s) => Some(s.as_str()),
            _ => None,
        }
    }
}

/// Context passed when decoding property values.
pub struct ValueParseCtx<'a> {
    pub line: usize,
    pub tzid: Option<&'a str>,
    pub mode: ParseMode,
    pub collect_warnings: bool,
}

/// Parse a raw property value string into [`PropertyValue`] according to property semantics,
/// optional `VALUE=` override, optional `TZID=`, and strict/lenient mode.
pub fn parse_property_value(
    ctx: &ValueParseCtx<'_>,
    name: &PropertyName,
    explicit_value_type: Option<&ValueType>,
    raw: &str,
    warnings: &mut Vec<ParseWarning>,
) -> Result<PropertyValue, ParseError> {
    if matches!(name, PropertyName::RequestStatus) && explicit_value_type.is_none() {
        return Ok(PropertyValue::RequestStatus(split_escaped_semicolon_list(
            raw,
        )));
    }

    if matches!(name, PropertyName::ExDate | PropertyName::RDate) {
        return parse_date_or_datetime_or_period_list(
            ctx,
            name,
            explicit_value_type,
            raw,
            warnings,
        );
    }

    if matches!(name, PropertyName::FreeBusy)
        && matches!(explicit_value_type, None | Some(ValueType::Period))
    {
        return parse_period_list_or_single(ctx, name, raw, warnings);
    }

    let default_vt = name.default_value_type();
    let effective_vt = explicit_value_type.unwrap_or(&default_vt);

    if matches!(effective_vt, ValueType::DateOrDateTimeList) {
        return parse_date_or_datetime_or_period_list(
            ctx,
            name,
            explicit_value_type,
            raw,
            warnings,
        );
    }

    match parse_scalar_value(ctx, name, effective_vt, raw, warnings) {
        Ok(val) => Ok(val),
        Err(detail) => fallback_or_err(ctx, name, raw, &detail, warnings),
    }
}

fn parse_scalar_value(
    ctx: &ValueParseCtx<'_>,
    name: &PropertyName,
    effective_vt: &ValueType,
    raw: &str,
    warnings: &mut Vec<ParseWarning>,
) -> Result<PropertyValue, String> {
    match effective_vt {
        ValueType::Text => Ok(PropertyValue::Text(CompactString::from(
            unescape_text(raw).as_ref(),
        ))),
        ValueType::TextList => Ok(PropertyValue::TextList(split_escaped_list(raw))),
        ValueType::Date => CalDate::parse_bytes(raw.trim().as_bytes())
            .map(PropertyValue::Date)
            .map_err(|e| e.to_string()),
        ValueType::DateTime => {
            parse_datetime_with_tz_recovery(ctx, raw.trim(), warnings).map(PropertyValue::DateTime)
        }
        ValueType::Time => CalTime::parse_bytes(raw.trim().as_bytes())
            .map(PropertyValue::Time)
            .map_err(|e| e.to_string()),
        ValueType::Duration => CalDuration::parse_bytes(raw.trim().as_bytes())
            .map(PropertyValue::Duration)
            .map_err(|e| e.to_string()),
        ValueType::Period => {
            let period = CalPeriod::parse_bytes(raw.trim().as_bytes(), ctx.tzid)
                .map_err(|e| e.to_string())?;
            check_period_non_zero(&period, ctx, name, warnings)?;
            Ok(PropertyValue::Period(period))
        }
        ValueType::Integer => raw
            .trim()
            .parse::<i64>()
            .map(PropertyValue::Integer)
            .map_err(|e| e.to_string()),
        ValueType::Float => raw
            .trim()
            .parse::<f64>()
            .map(PropertyValue::Float)
            .map_err(|e| e.to_string()),
        ValueType::Geo => parse_geo(raw.trim()),
        ValueType::Boolean => parse_bool(raw.trim()),
        ValueType::UtcOffset => UtcOffset::parse_bytes(raw.trim().as_bytes())
            .map(PropertyValue::UtcOffset)
            .map_err(|e| e.to_string()),
        ValueType::Uri => Ok(PropertyValue::Uri(CompactString::from(raw))),
        ValueType::CalAddress => Ok(PropertyValue::CalAddress(CompactString::from(raw))),
        ValueType::Binary => Ok(PropertyValue::Binary(CompactString::from(raw))),
        ValueType::Recur => Ok(PropertyValue::Recur(CompactString::from(raw))),
        ValueType::DateOrDateTimeList | ValueType::Unknown(_) => {
            Ok(PropertyValue::Raw(CompactString::from(raw)))
        }
    }
}

fn fallback_or_err(
    ctx: &ValueParseCtx<'_>,
    name: &PropertyName,
    raw: &str,
    detail: &str,
    warnings: &mut Vec<ParseWarning>,
) -> Result<PropertyValue, ParseError> {
    match ctx.mode {
        ParseMode::Strict => Err(ParseError::new(
            ctx.line,
            1,
            ParseErrorKind::InvalidValue {
                property: CompactString::from(name.as_str()),
                detail: CompactString::from(detail),
            },
        )),
        ParseMode::Lenient => {
            if ctx.collect_warnings {
                warnings.push(ParseWarning {
                    line: ctx.line,
                    message: CompactString::from(format!(
                        "invalid {} value fell back to raw text: {}",
                        name.as_str(),
                        detail
                    )),
                });
            }
            Ok(PropertyValue::Raw(CompactString::from(raw)))
        }
    }
}

fn parse_datetime_with_tz_recovery(
    ctx: &ValueParseCtx<'_>,
    raw: &str,
    warnings: &mut Vec<ParseWarning>,
) -> Result<CalDateTime, String> {
    match CalDateTime::parse_bytes(raw.as_bytes(), ctx.tzid) {
        Ok(dt) => Ok(dt),
        Err(DateTimeError::ConflictingUtcAndTzid) if ctx.mode == ParseMode::Lenient => {
            if ctx.collect_warnings {
                warnings.push(ParseWarning {
                    line: ctx.line,
                    message: CompactString::const_new(
                        "DATE-TIME has both 'Z' UTC suffix and TZID; treating as UTC",
                    ),
                });
            }
            CalDateTime::parse_bytes(raw.as_bytes(), None).map_err(|e| e.to_string())
        }
        Err(e) => Err(e.to_string()),
    }
}

fn parse_date_or_datetime_or_period_list(
    ctx: &ValueParseCtx<'_>,
    name: &PropertyName,
    explicit_value_type: Option<&ValueType>,
    raw: &str,
    warnings: &mut Vec<ParseWarning>,
) -> Result<PropertyValue, ParseError> {
    let trimmed = raw.trim();
    if trimmed.is_empty() {
        return Ok(PropertyValue::Raw(CompactString::from(raw)));
    }

    let items: Vec<&str> = trimmed.split(',').map(str::trim).collect();
    let infer_period = matches!(explicit_value_type, Some(ValueType::Period));
    let infer_date = matches!(explicit_value_type, Some(ValueType::Date))
        || (explicit_value_type.is_none() && items.first().is_some_and(|s| s.len() == 8));

    let res: Result<PropertyValue, String> = if infer_period {
        items
            .iter()
            .map(|item| {
                let period =
                    CalPeriod::parse_bytes(item.as_bytes(), ctx.tzid).map_err(|e| e.to_string())?;
                check_period_non_zero(&period, ctx, name, warnings)?;
                Ok(period)
            })
            .collect::<Result<Vec<_>, _>>()
            .map(PropertyValue::PeriodList)
    } else if infer_date {
        items
            .iter()
            .map(|item| CalDate::parse_bytes(item.as_bytes()).map_err(|e| e.to_string()))
            .collect::<Result<Vec<_>, _>>()
            .map(PropertyValue::DateList)
    } else {
        items
            .iter()
            .map(|item| parse_datetime_with_tz_recovery(ctx, item, warnings))
            .collect::<Result<Vec<_>, _>>()
            .map(PropertyValue::DateTimeList)
    };

    match res {
        Ok(val) => Ok(val),
        Err(detail) => fallback_or_err(ctx, name, raw, &detail, warnings),
    }
}

fn parse_period_list_or_single(
    ctx: &ValueParseCtx<'_>,
    name: &PropertyName,
    raw: &str,
    warnings: &mut Vec<ParseWarning>,
) -> Result<PropertyValue, ParseError> {
    let items: Vec<&str> = raw.trim().split(',').map(str::trim).collect();
    let mut out = Vec::with_capacity(items.len());
    for item in items {
        match CalPeriod::parse_bytes(item.as_bytes(), ctx.tzid) {
            Ok(p) => {
                if let Err(detail) = check_period_non_zero(&p, ctx, name, warnings) {
                    return fallback_or_err(ctx, name, raw, &detail, warnings);
                }
                out.push(p);
            }
            Err(e) => {
                return fallback_or_err(ctx, name, raw, &e.to_string(), warnings);
            }
        }
    }
    if out.len() == 1 {
        Ok(PropertyValue::Period(out.into_iter().next().unwrap()))
    } else {
        Ok(PropertyValue::PeriodList(out))
    }
}

fn check_period_non_zero(
    period: &CalPeriod,
    ctx: &ValueParseCtx<'_>,
    name: &PropertyName,
    warnings: &mut Vec<ParseWarning>,
) -> Result<(), String> {
    if !period.is_zero_or_negative() {
        return Ok(());
    }
    match ctx.mode {
        ParseMode::Strict => Err("PERIOD must have positive non-zero duration".to_owned()),
        ParseMode::Lenient => {
            if ctx.collect_warnings {
                warnings.push(ParseWarning {
                    line: ctx.line,
                    message: CompactString::from(format!(
                        "zero-length or negative PERIOD on {}",
                        name.as_str()
                    )),
                });
            }
            Ok(())
        }
    }
}

fn parse_geo(raw: &str) -> Result<PropertyValue, String> {
    let Some((lat_s, lon_s)) = raw.split_once(';') else {
        return Err("GEO must contain ';' separating latitude and longitude".to_owned());
    };
    let lat = lat_s.trim().parse::<f64>().map_err(|e| e.to_string())?;
    let lon = lon_s.trim().parse::<f64>().map_err(|e| e.to_string())?;
    if !(-90.0..=90.0).contains(&lat) || !(-180.0..=180.0).contains(&lon) {
        return Err("GEO coordinates out of range".to_owned());
    }
    Ok(PropertyValue::Geo { lat, lon })
}

fn parse_bool(raw: &str) -> Result<PropertyValue, String> {
    if raw.eq_ignore_ascii_case("TRUE") {
        Ok(PropertyValue::Boolean(true))
    } else if raw.eq_ignore_ascii_case("FALSE") {
        Ok(PropertyValue::Boolean(false))
    } else {
        Err(format!("expected TRUE or FALSE, found `{raw}`"))
    }
}
