//! RFC 5545 recurrence rule (`RRULE`), exception resolution, and interval-pruned query engine.
//!
//! Full implementation arrives in Phase 3 of the `icaloxide` roadmap.

#![forbid(unsafe_code)]

pub use icaloxide_core::Calendar;

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn workspace_core_link_works() {
        let cal = Calendar::new();
        assert_eq!(cal.component_count(), 1);
    }
}
