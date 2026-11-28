pub mod series;

pub use series::{
    compute_end_local, resolve_datetime_with_fallback, resolve_until_utc, CompiledSeries,
    ExclusionSet, RDateEntry, SpanDuration,
};
