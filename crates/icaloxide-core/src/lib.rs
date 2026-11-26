//! High-performance, zero-regex RFC 5545 / 7986 / 9073 / 7953 iCalendar core library.

#![forbid(unsafe_code)]

pub mod datetime;
pub mod error;
pub mod lexer;
pub mod model;
pub mod parser;
pub mod writer;

pub use datetime::{
    CalDate, CalDateTime, CalDuration, CalPeriod, CalTime, DateOrDateTime, TimeZoneRef, UtcOffset,
};
pub use error::{
    DateTimeError, ParseError, ParseErrorKind, ParseMode, ParseOptions, ParseWarning,
    SerializeError,
};
pub use model::{
    Calendar, ComponentArena, ComponentId, ComponentKind, ComponentMut, ComponentNode,
    ComponentRef, ParamName, Parameter, Property, PropertyName, PropertyValue, ValueType,
};
