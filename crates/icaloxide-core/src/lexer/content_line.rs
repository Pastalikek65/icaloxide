use std::borrow::Cow;

use smallvec::SmallVec;

use crate::error::{ParseError, ParseErrorKind};

/// A parsed property parameter on a content line.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RawParam<'a> {
    /// Raw parameter name (e.g. `TZID`, `CN`, `VALUE`).
    pub name: &'a str,
    /// One or more parameter values (comma-separated outside quotes, RFC 6868 unescaped).
    pub values: SmallVec<[Cow<'a, str>; 1]>,
}

/// A parsed RFC 5545 Section 3.1 content line: `name *(";" param) ":" value`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RawContentLine<'a> {
    /// 1-based physical line number where this logical line started.
    pub line: usize,
    /// Property name (e.g. `DTSTART`, `SUMMARY`, `BEGIN`).
    pub name: &'a str,
    /// Parsed parameters in source order.
    pub params: SmallVec<[RawParam<'a>; 2]>,
    /// Raw property value string (everything after the unquoted `:`).
    pub value: &'a str,
}

/// Parse a single unfolded logical line into [`RawContentLine`].
///
/// Correctly handles double-quoted parameter values containing `:`, `;`, or `,`,
/// multi-valued parameters (`MEMBER="a@x","b@y"`), and RFC 6868 `^` escaping.
pub fn parse_content_line(line_num: usize, input: &str) -> Result<RawContentLine<'_>, ParseError> {
    let bytes = input.as_bytes();
    let len = bytes.len();

    let mut i = 0;
    while i < len && bytes[i] != b';' && bytes[i] != b':' {
        i += 1;
    }

    if i == len {
        return Err(ParseError::new(
            line_num,
            len + 1,
            ParseErrorKind::MalformedContentLine("missing ':' value delimiter"),
        ));
    }

    let name = input[..i].trim();
    if name.is_empty() {
        return Err(ParseError::new(
            line_num,
            1,
            ParseErrorKind::MalformedContentLine("empty property name"),
        ));
    }

    let (params, value_start) = if bytes[i] == b';' {
        parse_params(line_num, input, i + 1)?
    } else {
        (SmallVec::new(), i + 1)
    };

    Ok(RawContentLine {
        line: line_num,
        name,
        params,
        value: &input[value_start..],
    })
}

fn parse_params(
    line_num: usize,
    input: &str,
    mut i: usize,
) -> Result<(SmallVec<[RawParam<'_>; 2]>, usize), ParseError> {
    let bytes = input.as_bytes();
    let len = bytes.len();
    let mut params: SmallVec<[RawParam<'_>; 2]> = SmallVec::new();

    loop {
        let param_start = i;
        while i < len && bytes[i] != b'=' && bytes[i] != b':' && bytes[i] != b';' {
            i += 1;
        }
        if i == len || bytes[i] != b'=' {
            return Err(ParseError::new(
                line_num,
                param_start + 1,
                ParseErrorKind::MalformedContentLine("parameter missing '='"),
            ));
        }
        let param_name = input[param_start..i].trim();
        if param_name.is_empty() {
            return Err(ParseError::new(
                line_num,
                param_start + 1,
                ParseErrorKind::MalformedContentLine("empty parameter name"),
            ));
        }
        i += 1; // skip `=`

        let parsed_vals = parse_param_values(line_num, input, i)?;
        params.push(RawParam {
            name: param_name,
            values: parsed_vals.values,
        });
        i = parsed_vals.next_idx;

        if parsed_vals.ended_with_colon {
            return Ok((params, i));
        }
        if i == len {
            return Err(ParseError::new(
                line_num,
                len + 1,
                ParseErrorKind::MalformedContentLine("missing ':' value delimiter"),
            ));
        }
    }
}

struct ParsedParamValues<'a> {
    values: SmallVec<[Cow<'a, str>; 1]>,
    next_idx: usize,
    ended_with_colon: bool,
}

fn parse_param_values(
    line_num: usize,
    input: &str,
    mut i: usize,
) -> Result<ParsedParamValues<'_>, ParseError> {
    let bytes = input.as_bytes();
    let len = bytes.len();
    let mut values: SmallVec<[Cow<'_, str>; 1]> = SmallVec::new();
    let mut in_quotes = false;
    let mut val_start = i;

    while i < len {
        let b = bytes[i];
        if b == b'"' {
            in_quotes = !in_quotes;
            i += 1;
        } else if !in_quotes && b == b',' {
            values.push(unescape_param_value(&input[val_start..i]));
            i += 1;
            val_start = i;
        } else if !in_quotes && (b == b';' || b == b':') {
            values.push(unescape_param_value(&input[val_start..i]));
            return Ok(ParsedParamValues {
                values,
                next_idx: i + 1,
                ended_with_colon: b == b':',
            });
        } else {
            i += 1;
        }
    }

    if in_quotes {
        return Err(ParseError::new(
            line_num,
            val_start + 1,
            ParseErrorKind::UnterminatedQuote,
        ));
    }

    Err(ParseError::new(
        line_num,
        len + 1,
        ParseErrorKind::MalformedContentLine("missing ':' value delimiter"),
    ))
}

/// Strip surrounding double quotes (if present) and decode RFC 6868 `^` escapes.
///
/// RFC 6868 mappings:
/// - `^n` -> `\n`
/// - `^^` -> `^`
/// - `^'` -> `"`
fn unescape_param_value(raw: &str) -> Cow<'_, str> {
    let trimmed = if raw.len() >= 2 && raw.starts_with('"') && raw.ends_with('"') {
        &raw[1..raw.len() - 1]
    } else {
        raw
    };

    if !trimmed.as_bytes().contains(&b'^') {
        return Cow::Borrowed(trimmed);
    }

    let mut out = String::with_capacity(trimmed.len());
    let mut chars = trimmed.chars();
    while let Some(c) = chars.next() {
        if c == '^' {
            match chars.next() {
                Some('n') => out.push('\n'),
                Some('^') | None => out.push('^'),
                Some('\'') => out.push('"'),
                Some(other) => {
                    out.push('^');
                    out.push(other);
                }
            }
        } else {
            out.push(c);
        }
    }
    Cow::Owned(out)
}
