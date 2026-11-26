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
            Err(_) => match self.mode {
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
            },
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
            Err(_) => match self.mode {
                ParseMode::Strict => Err(ParseError::new(line, 1, ParseErrorKind::InvalidUtf8)),
                ParseMode::Lenient => {
                    if collect_warnings {
                        warnings.push(ParseWarning {
                            line,
                            message: CompactString::const_new(
                                "invalid UTF-8 bytes in folded line replaced with U+FFFD",
                            ),
                        });
                    }
                    Ok(UnfoldedLine {
                        line,
                        text: Cow::Owned(String::from_utf8_lossy(&self.scratch).into_owned()),
                    })
                }
            },
        }
    }
}
