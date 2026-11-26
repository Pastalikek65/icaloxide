use compact_str::CompactString;
use thiserror::Error;

/// Parsing strictness mode.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum ParseMode {
    /// Enforce strict RFC 5545 compliance; reject malformed lines, invalid values,
    /// conflicting timezone indicators, or unclosed components.
    Strict,
    /// Recover gracefully from common real-world client quirks (Google, Apple,
    /// Outlook, Thunderbird) and record structured [`ParseWarning`] entries.
    #[default]
    Lenient,
}

/// Configuration options for parsing an iCalendar stream.
#[derive(Debug, Clone)]
pub struct ParseOptions {
    /// Strictness mode (`Strict` or `Lenient`).
    pub mode: ParseMode,
    /// Maximum allowed component nesting depth (guards against stack/OOM abuse).
    pub max_depth: usize,
    /// Maximum total number of components allowed in a single parse call.
    pub max_components: usize,
    /// Maximum byte length of a single unfolded logical content line.
    pub max_line_bytes: usize,
    /// Whether to retain [`ParseWarning`] diagnostics during lenient parsing.
    pub collect_warnings: bool,
}

impl Default for ParseOptions {
    fn default() -> Self {
        Self {
            mode: ParseMode::Lenient,
            max_depth: 32,
            max_components: 1_000_000,
            max_line_bytes: 4 * 1024 * 1024,
            collect_warnings: true,
        }
    }
}

impl ParseOptions {
    /// Create strict RFC 5545 parsing options with default resource limits.
    pub fn strict() -> Self {
        Self {
            mode: ParseMode::Strict,
            ..Self::default()
        }
    }

    /// Create lenient parsing options with default resource limits.
    pub fn lenient() -> Self {
        Self {
            mode: ParseMode::Lenient,
            ..Self::default()
        }
    }
}

/// Non-fatal diagnostic emitted during lenient parsing.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ParseWarning {
    /// 1-based physical line number where the issue originated.
    pub line: usize,
    /// Human-readable description of the recovery or quirk.
    pub message: CompactString,
}

/// Structured error categories returned when parsing fails.
#[derive(Debug, Error, Clone, PartialEq, Eq)]
pub enum ParseErrorKind {
    #[error("invalid UTF-8 sequence in input")]
    InvalidUtf8,
    #[error("logical line exceeds maximum allowed length of {max_bytes} bytes")]
    LineTooLong { max_bytes: usize },
    #[error("malformed content line: {0}")]
    MalformedContentLine(&'static str),
    #[error("unclosed quoted parameter value")]
    UnterminatedQuote,
    #[error("component nesting depth exceeded limit ({max_depth})")]
    MaxDepthExceeded { max_depth: usize },
    #[error("component count exceeded limit ({max_components})")]
    MaxComponentsExceeded { max_components: usize },
    #[error("property `{name}` appeared outside any BEGIN/END component block")]
    PropertyOutsideComponent { name: CompactString },
    #[error("unexpected END:{found}; expected END:{expected}")]
    MismatchedEnd {
        expected: CompactString,
        found: CompactString,
    },
    #[error("unexpected END:{found} with no matching BEGIN")]
    UnexpectedEnd { found: CompactString },
    #[error("unclosed component BEGIN:{name} at end of input")]
    UnclosedComponent { name: CompactString },
    #[error("empty component name in BEGIN/END")]
    EmptyComponentName,
    #[error("expected VCALENDAR root component, found {found}")]
    MissingVCalendarRoot { found: CompactString },
    #[error("no VCALENDAR component found in input")]
    EmptyCalendar,
    #[error("invalid property value for `{property}`: {detail}")]
    InvalidValue {
        property: CompactString,
        detail: CompactString,
    },
}

/// Error returned when parsing an iCalendar stream fails.
#[derive(Debug, Error, Clone, PartialEq, Eq)]
#[error("line {line}, col {column}: {kind}")]
pub struct ParseError {
    /// 1-based physical line number.
    pub line: usize,
    /// 1-based column (or byte offset within line; 1 if line-level).
    pub column: usize,
    /// Specific parse failure reason.
    pub kind: ParseErrorKind,
}

impl ParseError {
    pub fn new(line: usize, column: usize, kind: ParseErrorKind) -> Self {
        Self { line, column, kind }
    }
}

/// Errors produced when parsing RFC 5545 temporal values.
#[derive(Debug, Error, Clone, PartialEq, Eq)]
pub enum DateTimeError {
    #[error("invalid length or format for {kind}")]
    InvalidFormat { kind: &'static str },
    #[error("invalid digit in {kind}")]
    InvalidDigit { kind: &'static str },
    #[error("date out of valid calendar range: {year:04}-{month:02}-{day:02}")]
    InvalidDate { year: i16, month: u8, day: u8 },
    #[error("time out of valid range: {hour:02}:{minute:02}:{second:02}")]
    InvalidTime { hour: u8, minute: u8, second: u8 },
    #[error("UTC 'Z' suffix cannot be combined with a TZID parameter")]
    ConflictingUtcAndTzid,
    #[error("invalid duration syntax")]
    InvalidDuration,
    #[error("invalid period syntax")]
    InvalidPeriod,
    #[error("invalid UTC offset syntax")]
    InvalidUtcOffset,
}

/// Errors produced when serializing a [`crate::Calendar`].
#[derive(Debug, Error, Clone, PartialEq, Eq)]
pub enum SerializeError {
    #[error("formatting failure: {0}")]
    Fmt(#[from] std::fmt::Error),
}
