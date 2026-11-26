use compact_str::CompactString;
use smallvec::SmallVec;

use crate::error::{ParseError, ParseErrorKind, ParseMode, ParseOptions, ParseWarning};
use crate::lexer::{parse_content_line, RawContentLine, UnfoldIter};
use crate::model::value::ValueParseCtx;
use crate::model::{
    parse_property_value, Calendar, ComponentArena, ComponentId, ComponentKind, ParamName,
    Parameter, Property, PropertyName, ValueType,
};

struct ParserState<'a> {
    options: &'a ParseOptions,
    arena: ComponentArena,
    stack: Vec<(ComponentId, ComponentKind)>,
    root: Option<ComponentId>,
    warnings: Vec<ParseWarning>,
}

impl<'a> ParserState<'a> {
    fn new(options: &'a ParseOptions) -> Self {
        Self {
            options,
            arena: ComponentArena::new(),
            stack: Vec::with_capacity(4),
            root: None,
            warnings: Vec::new(),
        }
    }

    fn warn(&mut self, line: usize, msg: impl Into<CompactString>) {
        if self.options.collect_warnings {
            self.warnings.push(ParseWarning {
                line,
                message: msg.into(),
            });
        }
    }

    /// Returns `Ok(true)` to continue, `Ok(false)` to stop parsing after trailing root.
    fn handle_begin(&mut self, line_num: usize, raw_val: &str) -> Result<bool, ParseError> {
        let comp_name = raw_val.trim();
        if comp_name.is_empty() {
            return Err(ParseError::new(
                line_num,
                1,
                ParseErrorKind::EmptyComponentName,
            ));
        }
        if self.stack.len() >= self.options.max_depth {
            return Err(ParseError::new(
                line_num,
                1,
                ParseErrorKind::MaxDepthExceeded {
                    max_depth: self.options.max_depth,
                },
            ));
        }
        if self.arena.len() >= self.options.max_components {
            return Err(ParseError::new(
                line_num,
                1,
                ParseErrorKind::MaxComponentsExceeded {
                    max_components: self.options.max_components,
                },
            ));
        }

        let kind = ComponentKind::parse(comp_name);
        if self.stack.is_empty() {
            if self.root.is_some() {
                if self.options.mode == ParseMode::Strict {
                    return Err(ParseError::new(
                        line_num,
                        1,
                        ParseErrorKind::MalformedContentLine(
                            "multiple root components in single calendar parse",
                        ),
                    ));
                }
                self.warn(
                    line_num,
                    CompactString::const_new("ignored trailing root component after VCALENDAR"),
                );
                return Ok(false);
            }
            if kind != ComponentKind::VCalendar {
                return Err(ParseError::new(
                    line_num,
                    1,
                    ParseErrorKind::MissingVCalendarRoot {
                        found: CompactString::from(kind.as_str()),
                    },
                ));
            }
            let id = self.arena.alloc(kind.clone(), None);
            self.root = Some(id);
            self.stack.push((id, kind));
        } else {
            let parent_id = self.stack.last().map(|(id, _)| *id);
            let id = self.arena.alloc(kind.clone(), parent_id);
            self.stack.push((id, kind));
        }
        Ok(true)
    }

    fn handle_end(&mut self, line_num: usize, raw_val: &str) -> Result<(), ParseError> {
        let comp_name = raw_val.trim();
        if comp_name.is_empty() {
            return Err(ParseError::new(
                line_num,
                1,
                ParseErrorKind::EmptyComponentName,
            ));
        }
        let found_kind = ComponentKind::parse(comp_name);
        let Some((_, expected_kind)) = self.stack.last() else {
            return match self.options.mode {
                ParseMode::Strict => Err(ParseError::new(
                    line_num,
                    1,
                    ParseErrorKind::UnexpectedEnd {
                        found: CompactString::from(found_kind.as_str()),
                    },
                )),
                ParseMode::Lenient => {
                    self.warn(
                        line_num,
                        format!("ignored unexpected END:{}", found_kind.as_str()),
                    );
                    Ok(())
                }
            };
        };

        if expected_kind != &found_kind {
            return match self.options.mode {
                ParseMode::Strict => Err(ParseError::new(
                    line_num,
                    1,
                    ParseErrorKind::MismatchedEnd {
                        expected: CompactString::from(expected_kind.as_str()),
                        found: CompactString::from(found_kind.as_str()),
                    },
                )),
                ParseMode::Lenient => {
                    if let Some(pos) = self.stack.iter().rposition(|(_, k)| k == &found_kind) {
                        self.warn(
                            line_num,
                            format!(
                                "auto-closed unclosed component(s) before END:{}",
                                found_kind.as_str()
                            ),
                        );
                        self.stack.truncate(pos);
                    } else {
                        self.warn(
                            line_num,
                            format!("ignored mismatched END:{}", found_kind.as_str()),
                        );
                    }
                    Ok(())
                }
            };
        }

        self.stack.pop();
        Ok(())
    }

    fn handle_property(
        &mut self,
        line_num: usize,
        raw_line: RawContentLine<'_>,
    ) -> Result<(), ParseError> {
        let prop_name = PropertyName::parse(raw_line.name);
        let Some(&(current_id, _)) = self.stack.last() else {
            return match self.options.mode {
                ParseMode::Strict => Err(ParseError::new(
                    line_num,
                    1,
                    ParseErrorKind::PropertyOutsideComponent {
                        name: CompactString::from(prop_name.as_str()),
                    },
                )),
                ParseMode::Lenient => {
                    self.warn(
                        line_num,
                        format!(
                            "ignored property `{}` outside component",
                            prop_name.as_str()
                        ),
                    );
                    Ok(())
                }
            };
        };

        let mut params: SmallVec<[Parameter; 2]> = SmallVec::with_capacity(raw_line.params.len());
        let mut explicit_value_type: Option<ValueType> = None;
        let mut tzid: Option<CompactString> = None;

        for rp in raw_line.params {
            let pname = ParamName::parse(rp.name);
            let mut values: SmallVec<[CompactString; 1]> = SmallVec::with_capacity(rp.values.len());
            for v in rp.values {
                values.push(CompactString::from(v.as_ref()));
            }
            if pname == ParamName::Value {
                if let Some(first) = values.first() {
                    explicit_value_type = Some(ValueType::parse(first.as_str()));
                }
            } else if pname == ParamName::TzId {
                if let Some(first) = values.first() {
                    tzid = Some(first.clone());
                }
            }
            params.push(Parameter {
                name: pname,
                values,
            });
        }

        let ctx = ValueParseCtx {
            line: line_num,
            tzid: tzid.as_deref(),
            mode: self.options.mode,
            collect_warnings: self.options.collect_warnings,
        };
        let value = parse_property_value(
            &ctx,
            &prop_name,
            explicit_value_type.as_ref(),
            raw_line.value,
            &mut self.warnings,
        )?;

        if let Some(node) = self.arena.get_mut(current_id) {
            node.properties.push(Property {
                name: prop_name,
                params,
                value,
            });
        }
        Ok(())
    }

    fn finish(mut self) -> Result<Calendar, ParseError> {
        if !self.stack.is_empty() {
            match self.options.mode {
                ParseMode::Strict => {
                    let (_, kind) = self.stack.pop().unwrap();
                    return Err(ParseError::new(
                        1,
                        1,
                        ParseErrorKind::UnclosedComponent {
                            name: CompactString::from(kind.as_str()),
                        },
                    ));
                }
                ParseMode::Lenient => {
                    if self.options.collect_warnings {
                        for (_, kind) in self.stack.iter().rev() {
                            self.warnings.push(ParseWarning {
                                line: 1,
                                message: CompactString::from(format!(
                                    "auto-closed unclosed BEGIN:{} at EOF",
                                    kind.as_str()
                                )),
                            });
                        }
                    }
                }
            }
        }

        let Some(root_id) = self.root else {
            return Err(ParseError::new(1, 1, ParseErrorKind::EmptyCalendar));
        };

        Ok(Calendar {
            arena: self.arena,
            root: root_id,
            warnings: self.warnings,
        })
    }
}

/// Parse a raw iCalendar byte stream into a [`Calendar`] according to [`ParseOptions`].
pub fn parse_calendar_with_options(
    input: &[u8],
    options: &ParseOptions,
) -> Result<Calendar, ParseError> {
    let mut unfolder = UnfoldIter::new(input, options.mode, options.max_line_bytes);
    let mut state = ParserState::new(options);

    while let Some(line_res) = unfolder.next_line(&mut state.warnings, options.collect_warnings) {
        let logical_line = line_res?;
        let line_num = logical_line.line;

        let raw_line = match parse_content_line(line_num, logical_line.text.as_ref()) {
            Ok(cl) => cl,
            Err(err) => match options.mode {
                ParseMode::Strict => return Err(err),
                ParseMode::Lenient => {
                    state.warn(
                        line_num,
                        format!("skipped malformed content line: {}", err.kind),
                    );
                    continue;
                }
            },
        };

        if raw_line.name.eq_ignore_ascii_case("BEGIN") {
            if !state.handle_begin(line_num, raw_line.value)? {
                break;
            }
        } else if raw_line.name.eq_ignore_ascii_case("END") {
            state.handle_end(line_num, raw_line.value)?;
        } else {
            state.handle_property(line_num, raw_line)?;
        }
    }

    state.finish()
}
