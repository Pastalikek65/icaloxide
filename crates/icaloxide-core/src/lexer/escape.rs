use std::borrow::Cow;

use compact_str::CompactString;

/// Unescape an RFC 5545 Section 3.3.11 `TEXT` value.
///
/// Fast-path returns [`Cow::Borrowed`] with zero allocations when no `\` exists.
pub fn unescape_text(raw: &str) -> Cow<'_, str> {
    if !raw.as_bytes().contains(&b'\\') {
        return Cow::Borrowed(raw);
    }

    let mut out = String::with_capacity(raw.len());
    let mut chars = raw.chars();
    while let Some(c) = chars.next() {
        if c == '\\' {
            match chars.next() {
                Some('n' | 'N') => out.push('\n'),
                Some('\\') | None => out.push('\\'),
                Some(';') => out.push(';'),
                Some(',') => out.push(','),
                Some(other) => {
                    out.push('\\');
                    out.push(other);
                }
            }
        } else {
            out.push(c);
        }
    }
    Cow::Owned(out)
}

/// Escape an RFC 5545 Section 3.3.11 `TEXT` value for serialization.
///
/// Escapes `\`, `;`, `,`, and `\n` (ignoring `\r` in CRLF pairs).
pub fn escape_text(text: &str) -> Cow<'_, str> {
    let needs_escape = text
        .bytes()
        .any(|b| matches!(b, b'\\' | b';' | b',' | b'\n' | b'\r'));
    if !needs_escape {
        return Cow::Borrowed(text);
    }

    let mut out = String::with_capacity(text.len() + 8);
    let mut chars = text.chars().peekable();
    while let Some(c) = chars.next() {
        match c {
            '\\' => out.push_str("\\\\"),
            ';' => out.push_str("\\;"),
            ',' => out.push_str("\\,"),
            '\r' => {
                if chars.peek() == Some(&'\n') {
                    let _ = chars.next();
                }
                out.push_str("\\n");
            }
            '\n' => out.push_str("\\n"),
            other => out.push(other),
        }
    }
    Cow::Owned(out)
}

/// Split a comma-separated RFC 5545 `TEXT` list (e.g. `CATEGORIES`, `RESOURCES`),
/// honoring `\,` escapes inside individual items.
pub fn split_escaped_list(raw: &str) -> Vec<CompactString> {
    if raw.is_empty() {
        return Vec::new();
    }

    if !raw.as_bytes().contains(&b'\\') {
        return raw.split(',').map(CompactString::from).collect();
    }

    let mut items = Vec::new();
    let mut current = String::new();
    let mut chars = raw.chars();

    while let Some(c) = chars.next() {
        match c {
            ',' => {
                items.push(CompactString::from(current.as_str()));
                current.clear();
            }
            '\\' => match chars.next() {
                Some('n' | 'N') => current.push('\n'),
                Some('\\') | None => current.push('\\'),
                Some(';') => current.push(';'),
                Some(',') => current.push(','),
                Some(other) => {
                    current.push('\\');
                    current.push(other);
                }
            },
            other => current.push(other),
        }
    }
    items.push(CompactString::from(current.as_str()));
    items
}
