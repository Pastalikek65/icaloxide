//! RFC 5545 recurrence rule (`RRULE`), exception resolution, and interval-pruned calendar query engine.

#![forbid(unsafe_code)]

pub mod error;
pub mod query;
pub mod recurrence;
pub mod rrule;

pub use error::{IterationBudget, RRuleError};
pub use query::{AfterIter, CalendarQuery, Occurrence, QueryOptions};
pub use rrule::{
    expand_rrule, ByWeekday, Frequency, LocalDateTime, RRule, RRuleIter, UntilValue, Weekday,
};
