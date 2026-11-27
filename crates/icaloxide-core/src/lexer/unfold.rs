use std::borrow::Cow;

use compact_str::CompactString;
use memchr::memchr2;

use crate::error::{ParseError, ParseErrorKind, ParseMode, ParseWarning};

/// A single logical content line yielded by [`UnfoldIter`].
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UnfoldedLine<'a> {
    /// 1-based starting physical line number in the source stream.
    pub line: usize,
    /// Decoded UTF-8 text of the unfolded line (borrowed zero-copy when unfolded).
    pub text: Cow<'a, str>,
}

/// Streaming RFC 5545 Section 3.1 line unfolder over raw bytes.
///
/// Key properties:
/// - Uses SIMD-accelerated `memchr2` to locate `\r` and `\n`.
/// - Unfolds raw bytes *before* UTF-8 validation so multi-byte characters split
///   across fold boundaries by non-compliant generators still decode cleanly.
/// - Borrows zero-copy (`Cow::Borrowed`) when a logical line spans a single physical line.
/// - Skips blank lines.
pub struct UnfoldIter<'a> {
    input: &'a [u8],
    pos: usize,
    physical_line: usize,
    mode: ParseMode,
    max_line_bytes: usize,
    scratch: Vec<u8>,
}

impl<'a> UnfoldIter<'a> {
    /// Create a new unfolding iterator over raw iCalendar bytes.
    pub fn new(input: &'a [u8], mode: ParseMode, max_line_bytes: usize) -> Self {
        let mut pos = 0;
        // Strip optional UTF-8 BOM.
        if input.starts_with(b"\xEF\xBB\xBF") {
            pos = 3;
        }
        Self {
            input,
            pos,
            physical_line: 1,
            mode,
            max_line_bytes,
            scratch: Vec::new(),
        }
    }

    /// Scan a single physical line ending at `\r\n`, `\n`, `\r`, or EOF.
    /// Returns `(line_bytes, next_pos)`.
    #[inline]
    fn next_physical_slice(&self, start: usize) -> (&'a [u8], usize) {
        let rest = &self.input[start..];
        match memchr2(b'\r', b'\n', rest) {
            Some(offset) => {
                let end = start + offset;
                let mut next = end + 1;
                if rest[offset] == b'\r' && next < self.input.len() && self.input[next] == b'\n' {
                    next += 1;
                }
                (&self.input[start..end], next)
            }
            None => (rest, self.input.len()),
        }
    }

    /// Yield the next non-empty logical line, recording any UTF-8 recovery warnings in `Lenient` mode.
    pub fn next_line(
        &mut self,
        warnings: &mut Vec<ParseWarning>,
        collect_warnings: bool,
    ) -> Option<Result<UnfoldedLine<'a>, ParseError>> {
        loop {
            if self.pos >= self.input.len() {
                return None;
            }

            let start_line = self.physical_line;
            let (first_slice, mut next_pos) = self.next_physical_slice(self.pos);
            self.physical_line += 1;

            // Skip empty physical lines unless followed by a continuation (which is rare,
            // and empty lines themselves are meaningless in iCalendar).
            if first_slice.is_empty() {
                self.pos = next_pos;
                continue;
            }

            if first_slice.len() > self.max_line_bytes {
                self.pos = next_pos;
                return Some(Err(ParseError::new(
                    start_line,
                    1,
                    ParseErrorKind::LineTooLong {
                        max_bytes: self.max_line_bytes,
                    },
                )));
            }

            // Fast check: does the next physical line start with SPACE (0x20) or HTAB (0x09)?
            let is_folded =
                next_pos < self.input.len() && matches!(self.input[next_pos], b' ' | b'\t');

            if !is_folded {
                self.pos = next_pos;
                return Some(self.decode_borrowed(
                    start_line,
                    first_slice,
                    warnings,
                    collect_warnings,
                ));
            }

            // Folded slow-path: assemble raw bytes into scratch buffer before UTF-8 decoding.
            self.scratch.clear();
            self.scratch.extend_from_slice(first_slice);

            while next_pos < self.input.len() && matches!(self.input[next_pos], b' ' | b'\t') {
                // Skip the single folding whitespace octet (SPACE or HTAB).
                let cont_start = next_pos + 1;
                let (cont_slice, after_cont) = self.next_physical_slice(cont_start);
                self.physical_line += 1;
                next_pos = after_cont;

                if self.scratch.len().saturating_add(cont_slice.len()) > self.max_line_bytes {
                    self.pos = next_pos;
                    return Some(Err(ParseError::new(
                        start_line,
                        1,
                        ParseErrorKind::LineTooLong {
                            max_bytes: self.max_line_bytes,
                        },
                    )));
                }
                self.scratch.extend_from_slice(cont_slice);
            }

            self.pos = next_pos;
            return Some(self.decode_owned(start_line, warnings, collect_warnings));
        }
    }

    fn decode_borrowed(
        &self,
        line: usize,
        bytes: &'a [u8],
        warnings: &mut Vec<ParseWarning>,
        collect_warnings: bool,
    ) -> Result<UnfoldedLine<'a>, ParseError> {
        match std::str::from_utf8(bytes) {
            Ok(s) => Ok(UnfoldedLine {
                line,
                text: Cow::Borrowed(s),
            }),
            Err(_) => self.decode_non_utf8(line, bytes, warnings, collect_warnings),
        }
    }

    fn decode_owned(
        &self,
        line: usize,
        warnings: &mut Vec<ParseWarning>,
        collect_warnings: bool,
    ) -> Result<UnfoldedLine<'a>, ParseError> {
        match std::str::from_utf8(&self.scratch) {
            Ok(s) => Ok(UnfoldedLine {
                line,
                text: Cow::Owned(s.to_owned()),
            }),
            Err(_) => self.decode_non_utf8(line, &self.scratch, warnings, collect_warnings),
        }
    }

    fn decode_non_utf8(
        &self,
        line: usize,
        bytes: &[u8],
        warnings: &mut Vec<ParseWarning>,
        collect_warnings: bool,
    ) -> Result<UnfoldedLine<'a>, ParseError> {
        if let Some(decoded) = decode_legacy_charset(bytes) {
            if collect_warnings {
                warnings.push(ParseWarning {
                    line,
                    message: CompactString::const_new(
                        "decoded non-UTF-8 line using declared CHARSET parameter",
                    ),
                });
            }
            return Ok(UnfoldedLine {
                line,
                text: Cow::Owned(decoded),
            });
        }
        match self.mode {
            ParseMode::Strict => Err(ParseError::new(line, 1, ParseErrorKind::InvalidUtf8)),
            ParseMode::Lenient => {
                if collect_warnings {
                    warnings.push(ParseWarning {
                        line,
                        message: CompactString::const_new(
                            "invalid UTF-8 bytes replaced with U+FFFD",
                        ),
                    });
                }
                Ok(UnfoldedLine {
                    line,
                    text: Cow::Owned(String::from_utf8_lossy(bytes).into_owned()),
                })
            }
        }
    }
}

#[derive(Clone, Copy)]
enum LegacyCharset {
    Iso88591,
    Windows1252,
}

const WIN1252_UPPER: [char; 32] = [
    '\u{20AC}', '\u{FFFD}', '\u{201A}', '\u{0192}', '\u{201E}', '\u{2026}', '\u{2020}', '\u{2021}',
    '\u{02C6}', '\u{2030}', '\u{0160}', '\u{2039}', '\u{0152}', '\u{FFFD}', '\u{017D}', '\u{FFFD}',
    '\u{FFFD}', '\u{2018}', '\u{2019}', '\u{201C}', '\u{201D}', '\u{2022}', '\u{2013}', '\u{2014}',
    '\u{02DC}', '\u{2122}', '\u{0161}', '\u{203A}', '\u{0153}', '\u{FFFD}', '\u{017E}', '\u{0178}',
];

fn detect_legacy_charset(bytes: &[u8]) -> Option<LegacyCharset> {
    let mut in_quotes = false;
    let mut colon_pos = bytes.len();
    for (i, &b) in bytes.iter().enumerate() {
        if b == b'"' {
            in_quotes = !in_quotes;
        } else if b == b':' && !in_quotes {
            colon_pos = i;
            break;
        }
    }
    let header = &bytes[..colon_pos];
    let semi = header.iter().position(|&b| b == b';')?;
    for part in header[semi + 1..].split(|&b| b == b';') {
        let trimmed = trim_ascii_bytes(part);
        if trimmed.len() > 8 && trimmed[..8].eq_ignore_ascii_case(b"CHARSET=") {
            let val = trim_ascii_bytes(&trimmed[8..]);
            let unquoted = if val.len() >= 2 && val.starts_with(b"\"") && val.ends_with(b"\"") {
                &val[1..val.len() - 1]
            } else {
                val
            };
            if unquoted.eq_ignore_ascii_case(b"ISO-8859-1")
                || unquoted.eq_ignore_ascii_case(b"LATIN1")
                || unquoted.eq_ignore_ascii_case(b"LATIN-1")
            {
                return Some(LegacyCharset::Iso88591);
            }
            if unquoted.eq_ignore_ascii_case(b"WINDOWS-1252")
                || unquoted.eq_ignore_ascii_case(b"CP1252")
            {
                return Some(LegacyCharset::Windows1252);
            }
        }
    }
    None
}

fn trim_ascii_bytes(mut s: &[u8]) -> &[u8] {
    while let Some((&b, rest)) = s.split_first() {
        if b.is_ascii_whitespace() {
            s = rest;
        } else {
            break;
        }
    }
    while let Some((&b, rest)) = s.split_last() {
        if b.is_ascii_whitespace() {
            s = rest;
        } else {
            break;
        }
    }
    s
}

fn decode_legacy_charset(bytes: &[u8]) -> Option<String> {
    let charset = detect_legacy_charset(bytes)?;
    let mut out = String::with_capacity(bytes.len() + 16);
    for &b in bytes {
        let ch = match (charset, b) {
            (LegacyCharset::Windows1252, 0x80..=0x9F) => WIN1252_UPPER[(b - 0x80) as usize],
            _ => char::from(b),
        };
        out.push(ch);
    }
    Some(out)
}
