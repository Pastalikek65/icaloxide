use compact_str::CompactString;
use smallvec::SmallVec;

use crate::error::{ParseError, ParseOptions, ParseWarning};
use crate::model::known::{ComponentKind, ParamName, PropertyName};
use crate::model::value::PropertyValue;
use crate::parser::parse_calendar_with_options;
use crate::writer::write_calendar;

/// Opaque arena index identifying a component inside [`Calendar`].
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct ComponentId(pub u32);

/// A property parameter (`NAME=val1,val2`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Parameter {
    pub name: ParamName,
    pub values: SmallVec<[CompactString; 1]>,
}

impl Parameter {
    pub fn new(name: ParamName, value: impl Into<CompactString>) -> Self {
        let mut values = SmallVec::new();
        values.push(value.into());
        Self { name, values }
    }
}

/// A single property belonging to a component.
#[derive(Debug, Clone, PartialEq)]
pub struct Property {
    pub name: PropertyName,
    pub params: SmallVec<[Parameter; 2]>,
    pub value: PropertyValue,
}

impl Property {
    pub fn new(name: PropertyName, value: PropertyValue) -> Self {
        Self {
            name,
            params: SmallVec::new(),
            value,
        }
    }

    #[must_use]
    pub fn with_param(mut self, param: Parameter) -> Self {
        self.params.push(param);
        self
    }

    /// Return the first value of parameter `param_name`, if present.
    pub fn param_first(&self, param_name: &ParamName) -> Option<&str> {
        self.params
            .iter()
            .find(|p| &p.name == param_name)
            .and_then(|p| p.values.first())
            .map(CompactString::as_str)
    }
}

/// Arena node storing one iCalendar component (`VCALENDAR`, `VEVENT`, `VALARM`, etc.).
#[derive(Debug, Clone)]
pub struct ComponentNode {
    pub kind: ComponentKind,
    pub parent: Option<ComponentId>,
    pub children: Vec<ComponentId>,
    pub properties: Vec<Property>,
}

/// Flat cache-friendly arena storing all components in a calendar.
#[derive(Debug, Clone, Default)]
pub struct ComponentArena {
    pub(crate) nodes: Vec<ComponentNode>,
}

impl ComponentArena {
    pub fn new() -> Self {
        Self { nodes: Vec::new() }
    }

    pub fn alloc(&mut self, kind: ComponentKind, parent: Option<ComponentId>) -> ComponentId {
        let id = ComponentId(self.nodes.len() as u32);
        self.nodes.push(ComponentNode {
            kind,
            parent,
            children: Vec::new(),
            properties: Vec::new(),
        });
        if let Some(pid) = parent {
            self.nodes[pid.0 as usize].children.push(id);
        }
        id
    }

    #[inline]
    pub fn get(&self, id: ComponentId) -> Option<&ComponentNode> {
        self.nodes.get(id.0 as usize)
    }

    #[inline]
    pub fn get_mut(&mut self, id: ComponentId) -> Option<&mut ComponentNode> {
        self.nodes.get_mut(id.0 as usize)
    }

    #[inline]
    pub fn len(&self) -> usize {
        self.nodes.len()
    }

    #[inline]
    pub fn is_empty(&self) -> bool {
        self.nodes.is_empty()
    }
}

/// Parsed or constructed iCalendar document (`VCALENDAR` root + arena of nested components).
#[derive(Debug, Clone)]
pub struct Calendar {
    pub(crate) arena: ComponentArena,
    pub(crate) root: ComponentId,
    pub(crate) warnings: Vec<ParseWarning>,
}

impl Default for Calendar {
    fn default() -> Self {
        Self::new()
    }
}

impl Calendar {
    /// Create a minimal valid `VCALENDAR` with `VERSION:2.0` and `PRODID:-//icaloxide//icaloxide//EN`.
    pub fn new() -> Self {
        let mut arena = ComponentArena::new();
        let root = arena.alloc(ComponentKind::VCalendar, None);
        if let Some(node) = arena.get_mut(root) {
            node.properties.push(Property::new(
                PropertyName::Version,
                PropertyValue::Text(CompactString::const_new("2.0")),
            ));
            node.properties.push(Property::new(
                PropertyName::ProdId,
                PropertyValue::Text(CompactString::const_new("-//icaloxide//icaloxide//EN")),
            ));
        }
        Self {
            arena,
            root,
            warnings: Vec::new(),
        }
    }

    /// Parse raw iCalendar bytes using default lenient options.
    pub fn parse(input: &[u8]) -> Result<Self, ParseError> {
        Self::parse_with_options(input, &ParseOptions::default())
    }

    /// Parse an iCalendar `&str` using default lenient options.
    pub fn parse_str(input: &str) -> Result<Self, ParseError> {
        Self::parse_with_options(input.as_bytes(), &ParseOptions::default())
    }

    /// Parse raw iCalendar bytes with custom [`ParseOptions`].
    pub fn parse_with_options(input: &[u8], options: &ParseOptions) -> Result<Self, ParseError> {
        parse_calendar_with_options(input, options)
    }

    /// Parse raw iCalendar bytes containing one or more sequential `VCALENDAR` components.
    pub fn parse_all(input: &[u8], options: &ParseOptions) -> Result<Vec<Self>, ParseError> {
        crate::parser::parse_all_calendars_with_options(input, options)
    }

    /// Root `VCALENDAR` component ID.
    #[inline]
    pub const fn root_id(&self) -> ComponentId {
        self.root
    }

    /// Borrow a read-only view of the root `VCALENDAR`.
    #[inline]
    pub fn root(&self) -> ComponentRef<'_> {
        ComponentRef {
            arena: &self.arena,
            id: self.root,
        }
    }

    /// Borrow a mutable view of the root `VCALENDAR`.
    #[inline]
    pub fn root_mut(&mut self) -> ComponentMut<'_> {
        ComponentMut {
            arena: &mut self.arena,
            id: self.root,
        }
    }

    /// Borrow any component by [`ComponentId`].
    #[inline]
    pub fn component(&self, id: ComponentId) -> Option<ComponentRef<'_>> {
        if (id.0 as usize) < self.arena.len() {
            Some(ComponentRef {
                arena: &self.arena,
                id,
            })
        } else {
            None
        }
    }

    /// Mutable borrow of any component by [`ComponentId`].
    #[inline]
    pub fn component_mut(&mut self, id: ComponentId) -> Option<ComponentMut<'_>> {
        if (id.0 as usize) < self.arena.len() {
            Some(ComponentMut {
                arena: &mut self.arena,
                id,
            })
        } else {
            None
        }
    }

    /// Total number of components in the arena (including root `VCALENDAR`).
    #[inline]
    pub fn component_count(&self) -> usize {
        self.arena.len()
    }

    /// Non-fatal diagnostics recorded during lenient parsing.
    #[inline]
    pub fn warnings(&self) -> &[ParseWarning] {
        &self.warnings
    }

    /// Add a direct child component of `kind` to the root `VCALENDAR` and return its [`ComponentId`].
    pub fn add_component(&mut self, kind: ComponentKind) -> ComponentId {
        self.arena.alloc(kind, Some(self.root))
    }

    /// Add a child component under a specific `parent` component.
    pub fn add_child_component(
        &mut self,
        parent: ComponentId,
        kind: ComponentKind,
    ) -> Option<ComponentId> {
        if (parent.0 as usize) < self.arena.len() {
            Some(self.arena.alloc(kind, Some(parent)))
        } else {
            None
        }
    }

    /// Iterate over top-level subcomponents matching `kind`.
    pub fn components_of_kind(
        &self,
        kind: ComponentKind,
    ) -> impl Iterator<Item = ComponentRef<'_>> {
        self.root().children().filter(move |c| c.kind() == &kind)
    }

    /// Iterate over all top-level `VEVENT` components.
    pub fn events(&self) -> impl Iterator<Item = ComponentRef<'_>> {
        self.components_of_kind(ComponentKind::VEvent)
    }

    /// Iterate over all top-level `VTODO` components.
    pub fn todos(&self) -> impl Iterator<Item = ComponentRef<'_>> {
        self.components_of_kind(ComponentKind::VTodo)
    }

    /// Iterate over all top-level `VJOURNAL` components.
    pub fn journals(&self) -> impl Iterator<Item = ComponentRef<'_>> {
        self.components_of_kind(ComponentKind::VJournal)
    }

    /// Iterate over all top-level `VTIMEZONE` components.
    pub fn timezones(&self) -> impl Iterator<Item = ComponentRef<'_>> {
        self.components_of_kind(ComponentKind::VTimezone)
    }

    /// Iterate over all top-level `VFREEBUSY` components.
    pub fn freebusies(&self) -> impl Iterator<Item = ComponentRef<'_>> {
        self.components_of_kind(ComponentKind::VFreeBusy)
    }

    /// Iterate over all top-level `VAVAILABILITY` components.
    pub fn availabilities(&self) -> impl Iterator<Item = ComponentRef<'_>> {
        self.components_of_kind(ComponentKind::VAvailability)
    }

    /// Calendar display name (`NAME` per RFC 7986, falling back to `X-WR-CALNAME`).
    pub fn name(&self) -> Option<&str> {
        let root = self.root();
        root.property_text(&PropertyName::Name)
            .or_else(|| root.property_text(&PropertyName::XWrCalName))
    }

    /// Calendar `PRODID`.
    pub fn prodid(&self) -> Option<&str> {
        self.root().property_text(&PropertyName::ProdId)
    }

    /// Build a timezone registry from this calendar's `VTIMEZONE`s, `X-WR-TIMEZONE`,
    /// bundled IANA TZDB, and Windows CLDR aliases.
    pub fn tz_registry(&self) -> crate::tz::TzRegistry {
        crate::tz::TzRegistry::from_calendar(self)
    }

    /// Inject missing RFC 5545 `VTIMEZONE` definitions for all non-UTC `TZID`s
    /// referenced in this calendar, returning the added `TZID`s.
    pub fn add_missing_timezones(&mut self) -> Vec<CompactString> {
        crate::tz::add_missing_timezones(self)
    }

    /// Serialize this calendar to an RFC 5545 folded string (`\r\n` endings, 75-octet limit).
    pub fn to_ical(&self) -> String {
        let mut out = String::with_capacity(self.arena.len() * 128);
        let _ = write_calendar(self, &mut out);
        out
    }

    /// Serialize a single component (e.g. a `VEVENT`) to an RFC 5545 string.
    pub fn component_to_ical(&self, id: ComponentId) -> Option<String> {
        self.component(id)?;
        let mut out = String::with_capacity(256);
        let _ = crate::writer::write_component(&self.arena, id, &mut out);
        Some(out)
    }
}

/// Lightweight borrowed handle to a component inside a [`Calendar`].
#[derive(Debug, Clone, Copy)]
pub struct ComponentRef<'a> {
    pub(crate) arena: &'a ComponentArena,
    pub(crate) id: ComponentId,
}

impl<'a> ComponentRef<'a> {
    #[inline]
    fn node(self) -> &'a ComponentNode {
        &self.arena.nodes[self.id.0 as usize]
    }

    #[inline]
    pub const fn id(self) -> ComponentId {
        self.id
    }

    #[inline]
    pub fn kind(self) -> &'a ComponentKind {
        &self.node().kind
    }

    #[inline]
    pub fn properties(self) -> &'a [Property] {
        &self.node().properties
    }

    #[inline]
    pub fn children(self) -> impl Iterator<Item = ComponentRef<'a>> + 'a {
        self.node().children.iter().map(move |&cid| ComponentRef {
            arena: self.arena,
            id: cid,
        })
    }

    /// First property matching `name`.
    pub fn property(self, name: &PropertyName) -> Option<&'a Property> {
        self.node().properties.iter().find(|p| &p.name == name)
    }

    /// All properties matching `name`.
    pub fn properties_named(
        self,
        name: &'a PropertyName,
    ) -> impl Iterator<Item = &'a Property> + 'a {
        self.node()
            .properties
            .iter()
            .filter(move |p| &p.name == name)
    }

    /// Return text string for the first matching property, if it is string-backed.
    pub fn property_text(self, name: &PropertyName) -> Option<&'a str> {
        self.property(name).and_then(|p| p.value.as_str())
    }

    pub fn uid(self) -> Option<&'a str> {
        self.property_text(&PropertyName::Uid)
    }

    pub fn summary(self) -> Option<&'a str> {
        self.property_text(&PropertyName::Summary)
    }

    pub fn description(self) -> Option<&'a str> {
        self.property_text(&PropertyName::Description)
    }

    pub fn location(self) -> Option<&'a str> {
        self.property_text(&PropertyName::Location)
    }

    pub fn status(self) -> Option<&'a str> {
        self.property_text(&PropertyName::Status)
    }
}

/// Mutable handle to a component inside a [`Calendar`].
pub struct ComponentMut<'a> {
    pub(crate) arena: &'a mut ComponentArena,
    pub(crate) id: ComponentId,
}

impl ComponentMut<'_> {
    #[inline]
    fn node_mut(&mut self) -> &mut ComponentNode {
        &mut self.arena.nodes[self.id.0 as usize]
    }

    #[inline]
    pub const fn id(&self) -> ComponentId {
        self.id
    }

    /// Append a property to this component.
    pub fn add_property(&mut self, prop: Property) {
        self.node_mut().properties.push(prop);
    }

    /// Replace all properties matching `prop.name` with `prop` (or append if absent).
    pub fn set_property(&mut self, prop: Property) {
        let props = &mut self.node_mut().properties;
        props.retain(|existing| existing.name != prop.name);
        props.push(prop);
    }

    /// Remove all properties matching `name` and return how many were removed.
    pub fn remove_property(&mut self, name: &PropertyName) -> usize {
        let props = &mut self.node_mut().properties;
        let before = props.len();
        props.retain(|existing| &existing.name != name);
        before - props.len()
    }
}
