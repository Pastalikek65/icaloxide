use compact_str::CompactString;
use thiserror::Error;

/// Safety limits guarding against pathological or infinite `RRULE` inputs.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct IterationBudget {
    /// Maximum number of recurrence periods inspected in a single expansion/iteration.
    pub max_periods: usize,
    /// Maximum consecutive empty recurrence periods before aborting (e.g., `BYMONTH=2;BYMONTHDAY=30`).
    pub max_empty_periods: usize,
}

impl Default for IterationBudget {
    fn default() -> Self {
        Self {
            max_periods: 500_000,
            max_empty_periods: 10_000,
        }
    }
}

/// Errors returned during `RRULE` parsing, recurrence resolution, or calendar querying.
#[derive(Debug, Error, Clone, PartialEq, Eq)]
pub enum RRuleError {
    #[error("RRULE missing required FREQ part")]
    MissingFreq,
    #[error("invalid FREQ value `{0}`")]
    InvalidFreq(CompactString),
    #[error("malformed RRULE part `{0}`")]
    InvalidPart(CompactString),
    #[error("invalid numeric value in RRULE: `{0}`")]
    InvalidNumber(CompactString),
    #[error("invalid weekday specification in RRULE: `{0}`")]
    InvalidWeekday(CompactString),
    #[error("invalid UNTIL value in RRULE: `{0}`")]
    InvalidUntil(CompactString),
    #[error("RRULE cannot specify both COUNT and UNTIL")]
    CountAndUntilConflict,
    #[error("RRULE expansion exceeded iteration safety budget")]
    BudgetExceeded,
    #[error("timezone error: {0}")]
    TimezoneError(#[from] icaloxide_core::TzError),
}
