pub mod series;

pub use series::{
    compute_end_local, resolve_datetime_with_fallback, CompiledSeries, ExclusionSet, RDateEntry,
    SpanDuration,
};
