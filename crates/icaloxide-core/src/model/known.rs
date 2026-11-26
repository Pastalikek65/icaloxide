use std::fmt;

use compact_str::CompactString;

/// Helper: uppercase ASCII string onto a stack buffer (up to 64 bytes) for O(1) match dispatch.
#[inline]
fn to_upper_ascii_buf<'a>(s: &str, buf: &'a mut [u8; 64]) -> Option<&'a str> {
    let bytes = s.as_bytes();
    if bytes.len() > buf.len() || !bytes.is_ascii() {
        return None;
    }
    for (dst, &src) in buf.iter_mut().zip(bytes.iter()) {
        *dst = src.to_ascii_uppercase();
    }
    std::str::from_utf8(&buf[..bytes.len()]).ok()
}

/// Known RFC 5545 / RFC 7986 / RFC 9073 / RFC 7953 component kinds.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub enum ComponentKind {
    VCalendar,
    VEvent,
    VTodo,
    VJournal,
    VFreeBusy,
    VTimezone,
    VAlarm,
    Standard,
    Daylight,
    /// RFC 7953 availability container.
    VAvailability,
    /// RFC 7953 availability window.
    Available,
    /// RFC 9073 structured location component.
    VLocation,
    /// RFC 9073 structured resource component.
    VResource,
    /// RFC 9073 participant component.
    Participant,
    /// Non-standard / X-component.
    Other(CompactString),
}

impl ComponentKind {
    /// Parse a component name case-insensitively with zero heap allocation for known components.
    pub fn parse(raw: &str) -> Self {
        let mut buf = [0_u8; 64];
        if let Some(upper) = to_upper_ascii_buf(raw, &mut buf) {
            return match upper {
                "VCALENDAR" => Self::VCalendar,
                "VEVENT" => Self::VEvent,
                "VTODO" => Self::VTodo,
                "VJOURNAL" => Self::VJournal,
                "VFREEBUSY" => Self::VFreeBusy,
                "VTIMEZONE" => Self::VTimezone,
                "VALARM" => Self::VAlarm,
                "STANDARD" => Self::Standard,
                "DAYLIGHT" => Self::Daylight,
                "VAVAILABILITY" => Self::VAvailability,
                "AVAILABLE" => Self::Available,
                "VLOCATION" => Self::VLocation,
                "VRESOURCE" => Self::VResource,
                "PARTICIPANT" => Self::Participant,
                _ => Self::Other(CompactString::from(upper)),
            };
        }
        Self::Other(CompactString::from(raw.to_ascii_uppercase()))
    }

    /// Canonical uppercase component name.
    pub fn as_str(&self) -> &str {
        match self {
            Self::VCalendar => "VCALENDAR",
            Self::VEvent => "VEVENT",
            Self::VTodo => "VTODO",
            Self::VJournal => "VJOURNAL",
            Self::VFreeBusy => "VFREEBUSY",
            Self::VTimezone => "VTIMEZONE",
            Self::VAlarm => "VALARM",
            Self::Standard => "STANDARD",
            Self::Daylight => "DAYLIGHT",
            Self::VAvailability => "VAVAILABILITY",
            Self::Available => "AVAILABLE",
            Self::VLocation => "VLOCATION",
            Self::VResource => "VRESOURCE",
            Self::Participant => "PARTICIPANT",
            Self::Other(s) => s.as_str(),
        }
    }
}

impl fmt::Display for ComponentKind {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

/// RFC 5545 Section 3.3 data value types.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub enum ValueType {
    Binary,
    Boolean,
    CalAddress,
    Date,
    DateTime,
    Duration,
    Float,
    Integer,
    Period,
    Recur,
    Text,
    Time,
    Uri,
    UtcOffset,
    /// Special compound `lat;lon` type for `GEO`.
    Geo,
    /// Comma-separated list of `TEXT` values (`CATEGORIES`, `RESOURCES`).
    TextList,
    /// Comma-separated list of `DATE` / `DATE-TIME` values (`EXDATE`, `RDATE`).
    DateOrDateTimeList,
    /// Unrecognized `VALUE=` parameter or untyped extension property.
    Unknown(CompactString),
}

impl ValueType {
    /// Parse a `VALUE=` parameter string case-insensitively.
    pub fn parse(raw: &str) -> Self {
        let mut buf = [0_u8; 64];
        if let Some(upper) = to_upper_ascii_buf(raw, &mut buf) {
            return match upper {
                "BINARY" => Self::Binary,
                "BOOLEAN" => Self::Boolean,
                "CAL-ADDRESS" => Self::CalAddress,
                "DATE" => Self::Date,
                "DATE-TIME" => Self::DateTime,
                "DURATION" => Self::Duration,
                "FLOAT" => Self::Float,
                "INTEGER" => Self::Integer,
                "PERIOD" => Self::Period,
                "RECUR" => Self::Recur,
                "TEXT" => Self::Text,
                "TIME" => Self::Time,
                "URI" => Self::Uri,
                "UTC-OFFSET" => Self::UtcOffset,
                _ => Self::Unknown(CompactString::from(upper)),
            };
        }
        Self::Unknown(CompactString::from(raw.to_ascii_uppercase()))
    }
}

/// Interned RFC 5545 / 7986 / 9073 / 7953 property names.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub enum PropertyName {
    // Calendar properties
    CalScale,
    Method,
    ProdId,
    Version,
    // Descriptive
    Attach,
    Categories,
    Class,
    Comment,
    Description,
    Geo,
    Location,
    PercentComplete,
    Priority,
    Resources,
    Status,
    Summary,
    // Date and Time
    Completed,
    DtEnd,
    Due,
    DtStart,
    Duration,
    FreeBusy,
    Transp,
    // Time Zone
    TzId,
    TzName,
    TzOffsetFrom,
    TzOffsetTo,
    TzUrl,
    // Relationship
    Attendee,
    Contact,
    Organizer,
    RecurrenceId,
    RelatedTo,
    Url,
    Uid,
    // Recurrence
    ExDate,
    RDate,
    RRule,
    // Alarm
    Action,
    Repeat,
    Trigger,
    // Change Management
    Created,
    DtStamp,
    LastModified,
    Sequence,
    // Misc
    RequestStatus,
    // RFC 7986 Modern Properties
    Name,
    RefreshInterval,
    Source,
    Color,
    Image,
    Conference,
    // RFC 7953 Availability
    BusyType,
    // RFC 9073 Event Publishing
    LocationType,
    ParticipantType,
    CalendarAddress,
    StructuredData,
    StyledDescription,
    // Widely deployed vendor properties
    XWrCalName,
    XWrTimeZone,
    XWrCalDesc,
    /// Any other `X-*` or extension property.
    Other(CompactString),
}

impl PropertyName {
    /// Parse a property name case-insensitively with zero heap allocation for standard names.
    pub fn parse(raw: &str) -> Self {
        let mut buf = [0_u8; 64];
        if let Some(upper) = to_upper_ascii_buf(raw, &mut buf) {
            return match upper {
                "CALSCALE" => Self::CalScale,
                "METHOD" => Self::Method,
                "PRODID" => Self::ProdId,
                "VERSION" => Self::Version,
                "ATTACH" => Self::Attach,
                "CATEGORIES" => Self::Categories,
                "CLASS" => Self::Class,
                "COMMENT" => Self::Comment,
                "DESCRIPTION" => Self::Description,
                "GEO" => Self::Geo,
                "LOCATION" => Self::Location,
                "PERCENT-COMPLETE" => Self::PercentComplete,
                "PRIORITY" => Self::Priority,
                "RESOURCES" => Self::Resources,
                "STATUS" => Self::Status,
                "SUMMARY" => Self::Summary,
                "COMPLETED" => Self::Completed,
                "DTEND" => Self::DtEnd,
                "DUE" => Self::Due,
                "DTSTART" => Self::DtStart,
                "DURATION" => Self::Duration,
                "FREEBUSY" => Self::FreeBusy,
                "TRANSP" => Self::Transp,
                "TZID" => Self::TzId,
                "TZNAME" => Self::TzName,
                "TZOFFSETFROM" => Self::TzOffsetFrom,
                "TZOFFSETTO" => Self::TzOffsetTo,
                "TZURL" => Self::TzUrl,
                "ATTENDEE" => Self::Attendee,
                "CONTACT" => Self::Contact,
                "ORGANIZER" => Self::Organizer,
                "RECURRENCE-ID" => Self::RecurrenceId,
                "RELATED-TO" => Self::RelatedTo,
                "URL" => Self::Url,
                "UID" => Self::Uid,
                "EXDATE" => Self::ExDate,
                "RDATE" => Self::RDate,
                "RRULE" => Self::RRule,
                "ACTION" => Self::Action,
                "REPEAT" => Self::Repeat,
                "TRIGGER" => Self::Trigger,
                "CREATED" => Self::Created,
                "DTSTAMP" => Self::DtStamp,
                "LAST-MODIFIED" => Self::LastModified,
                "SEQUENCE" => Self::Sequence,
                "REQUEST-STATUS" => Self::RequestStatus,
                "NAME" => Self::Name,
                "REFRESH-INTERVAL" => Self::RefreshInterval,
                "SOURCE" => Self::Source,
                "COLOR" => Self::Color,
                "IMAGE" => Self::Image,
                "CONFERENCE" => Self::Conference,
                "BUSYTYPE" => Self::BusyType,
                "LOCATION-TYPE" => Self::LocationType,
                "PARTICIPANT-TYPE" => Self::ParticipantType,
                "CALENDAR-ADDRESS" => Self::CalendarAddress,
                "STRUCTURED-DATA" => Self::StructuredData,
                "STYLED-DESCRIPTION" => Self::StyledDescription,
                "X-WR-CALNAME" => Self::XWrCalName,
                "X-WR-TIMEZONE" => Self::XWrTimeZone,
                "X-WR-CALDESC" => Self::XWrCalDesc,
                _ => Self::Other(CompactString::from(upper)),
            };
        }
        Self::Other(CompactString::from(raw.to_ascii_uppercase()))
    }

    /// Canonical uppercase RFC property name.
    pub fn as_str(&self) -> &str {
        match self {
            Self::CalScale => "CALSCALE",
            Self::Method => "METHOD",
            Self::ProdId => "PRODID",
            Self::Version => "VERSION",
            Self::Attach => "ATTACH",
            Self::Categories => "CATEGORIES",
            Self::Class => "CLASS",
            Self::Comment => "COMMENT",
            Self::Description => "DESCRIPTION",
            Self::Geo => "GEO",
            Self::Location => "LOCATION",
            Self::PercentComplete => "PERCENT-COMPLETE",
            Self::Priority => "PRIORITY",
            Self::Resources => "RESOURCES",
            Self::Status => "STATUS",
            Self::Summary => "SUMMARY",
            Self::Completed => "COMPLETED",
            Self::DtEnd => "DTEND",
            Self::Due => "DUE",
            Self::DtStart => "DTSTART",
            Self::Duration => "DURATION",
            Self::FreeBusy => "FREEBUSY",
            Self::Transp => "TRANSP",
            Self::TzId => "TZID",
            Self::TzName => "TZNAME",
            Self::TzOffsetFrom => "TZOFFSETFROM",
            Self::TzOffsetTo => "TZOFFSETTO",
            Self::TzUrl => "TZURL",
            Self::Attendee => "ATTENDEE",
            Self::Contact => "CONTACT",
            Self::Organizer => "ORGANIZER",
            Self::RecurrenceId => "RECURRENCE-ID",
            Self::RelatedTo => "RELATED-TO",
            Self::Url => "URL",
            Self::Uid => "UID",
            Self::ExDate => "EXDATE",
            Self::RDate => "RDATE",
            Self::RRule => "RRULE",
            Self::Action => "ACTION",
            Self::Repeat => "REPEAT",
            Self::Trigger => "TRIGGER",
            Self::Created => "CREATED",
            Self::DtStamp => "DTSTAMP",
            Self::LastModified => "LAST-MODIFIED",
            Self::Sequence => "SEQUENCE",
            Self::RequestStatus => "REQUEST-STATUS",
            Self::Name => "NAME",
            Self::RefreshInterval => "REFRESH-INTERVAL",
            Self::Source => "SOURCE",
            Self::Color => "COLOR",
            Self::Image => "IMAGE",
            Self::Conference => "CONFERENCE",
            Self::BusyType => "BUSYTYPE",
            Self::LocationType => "LOCATION-TYPE",
            Self::ParticipantType => "PARTICIPANT-TYPE",
            Self::CalendarAddress => "CALENDAR-ADDRESS",
            Self::StructuredData => "STRUCTURED-DATA",
            Self::StyledDescription => "STYLED-DESCRIPTION",
            Self::XWrCalName => "X-WR-CALNAME",
            Self::XWrTimeZone => "X-WR-TIMEZONE",
            Self::XWrCalDesc => "X-WR-CALDESC",
            Self::Other(s) => s.as_str(),
        }
    }

    /// Default RFC value type when no explicit `VALUE=` parameter is present.
    pub fn default_value_type(&self) -> ValueType {
        match self {
            Self::CalScale
            | Self::Method
            | Self::ProdId
            | Self::Version
            | Self::Class
            | Self::Comment
            | Self::Description
            | Self::Location
            | Self::Status
            | Self::Summary
            | Self::Transp
            | Self::TzId
            | Self::TzName
            | Self::Contact
            | Self::RelatedTo
            | Self::Uid
            | Self::Action
            | Self::RequestStatus
            | Self::Name
            | Self::Color
            | Self::BusyType
            | Self::LocationType
            | Self::ParticipantType
            | Self::StyledDescription
            | Self::StructuredData
            | Self::XWrCalName
            | Self::XWrTimeZone
            | Self::XWrCalDesc => ValueType::Text,

            Self::Categories | Self::Resources => ValueType::TextList,

            Self::Attach
            | Self::TzUrl
            | Self::Url
            | Self::Source
            | Self::Image
            | Self::Conference => ValueType::Uri,
            Self::Attendee | Self::Organizer | Self::CalendarAddress => ValueType::CalAddress,

            Self::Completed
            | Self::DtEnd
            | Self::Due
            | Self::DtStart
            | Self::RecurrenceId
            | Self::Created
            | Self::DtStamp
            | Self::LastModified => ValueType::DateTime,

            Self::ExDate | Self::RDate => ValueType::DateOrDateTimeList,

            Self::Duration | Self::Trigger | Self::RefreshInterval => ValueType::Duration,
            Self::FreeBusy => ValueType::Period,
            Self::Geo => ValueType::Geo,
            Self::PercentComplete | Self::Priority | Self::Repeat | Self::Sequence => {
                ValueType::Integer
            }
            Self::TzOffsetFrom | Self::TzOffsetTo => ValueType::UtcOffset,
            Self::RRule => ValueType::Recur,
            Self::Other(name) => ValueType::Unknown(name.clone()),
        }
    }
}

impl fmt::Display for PropertyName {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

/// Interned RFC 5545 / 7986 / 9073 parameter names.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub enum ParamName {
    AltRep,
    Cn,
    CuType,
    DelFrom,
    DelTo,
    Dir,
    Encoding,
    FmtType,
    FbType,
    Language,
    Member,
    PartStat,
    Range,
    Related,
    RelType,
    Role,
    Rsvp,
    SentBy,
    TzId,
    Value,
    // RFC 7986 / 9073
    Display,
    Email,
    Feature,
    Label,
    Order,
    Schema,
    Derived,
    /// Any `X-*` or custom parameter.
    Other(CompactString),
}

impl ParamName {
    /// Parse a parameter name case-insensitively.
    pub fn parse(raw: &str) -> Self {
        let mut buf = [0_u8; 64];
        if let Some(upper) = to_upper_ascii_buf(raw, &mut buf) {
            return match upper {
                "ALTREP" => Self::AltRep,
                "CN" => Self::Cn,
                "CUTYPE" => Self::CuType,
                "DELEGATED-FROM" => Self::DelFrom,
                "DELEGATED-TO" => Self::DelTo,
                "DIR" => Self::Dir,
                "ENCODING" => Self::Encoding,
                "FMTTYPE" => Self::FmtType,
                "FBTYPE" => Self::FbType,
                "LANGUAGE" => Self::Language,
                "MEMBER" => Self::Member,
                "PARTSTAT" => Self::PartStat,
                "RANGE" => Self::Range,
                "RELATED" => Self::Related,
                "RELTYPE" => Self::RelType,
                "ROLE" => Self::Role,
                "RSVP" => Self::Rsvp,
                "SENT-BY" => Self::SentBy,
                "TZID" => Self::TzId,
                "VALUE" => Self::Value,
                "DISPLAY" => Self::Display,
                "EMAIL" => Self::Email,
                "FEATURE" => Self::Feature,
                "LABEL" => Self::Label,
                "ORDER" => Self::Order,
                "SCHEMA" => Self::Schema,
                "DERIVED" => Self::Derived,
                _ => Self::Other(CompactString::from(upper)),
            };
        }
        Self::Other(CompactString::from(raw.to_ascii_uppercase()))
    }

    /// Canonical uppercase parameter name.
    pub fn as_str(&self) -> &str {
        match self {
            Self::AltRep => "ALTREP",
            Self::Cn => "CN",
            Self::CuType => "CUTYPE",
            Self::DelFrom => "DELEGATED-FROM",
            Self::DelTo => "DELEGATED-TO",
            Self::Dir => "DIR",
            Self::Encoding => "ENCODING",
            Self::FmtType => "FMTTYPE",
            Self::FbType => "FBTYPE",
            Self::Language => "LANGUAGE",
            Self::Member => "MEMBER",
            Self::PartStat => "PARTSTAT",
            Self::Range => "RANGE",
            Self::Related => "RELATED",
            Self::RelType => "RELTYPE",
            Self::Role => "ROLE",
            Self::Rsvp => "RSVP",
            Self::SentBy => "SENT-BY",
            Self::TzId => "TZID",
            Self::Value => "VALUE",
            Self::Display => "DISPLAY",
            Self::Email => "EMAIL",
            Self::Feature => "FEATURE",
            Self::Label => "LABEL",
            Self::Order => "ORDER",
            Self::Schema => "SCHEMA",
            Self::Derived => "DERIVED",
            Self::Other(s) => s.as_str(),
        }
    }
}

impl fmt::Display for ParamName {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}
