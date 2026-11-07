use crate::parse_pack_version;

/// Runtime variant encoded in an official GTNH server-pack name.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum PackVariant {
    Java8,
    Java17,
}

impl PackVariant {
    /// Return the stable variant identifier used by Crucible's Python code.
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Java8 => "java8",
            Self::Java17 => "java17_2X",
        }
    }
}

/// Version and Java variant extracted from a GTNH server-pack name.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct GtnhServerName {
    /// Preserve the trimmed source spelling, including prerelease case/zeroes.
    pub version: String,
    pub variant: PackVariant,
}

fn word_at(bytes: &[u8], position: usize, word: &[u8]) -> bool {
    let Some(end) = position.checked_add(word.len()) else {
        return false;
    };
    bytes
        .get(position..end)
        .is_some_and(|candidate| candidate.eq_ignore_ascii_case(word))
}

fn is_name_separator(byte: u8) -> bool {
    matches!(byte, b' ' | b'_')
}

fn consume_optional_separator(bytes: &[u8], position: &mut usize) {
    if bytes
        .get(*position)
        .is_some_and(|byte| is_name_separator(*byte))
    {
        *position += 1;
    }
}

fn consume_required_separators(bytes: &[u8], position: &mut usize) -> bool {
    let start = *position;
    while bytes
        .get(*position)
        .is_some_and(|byte| is_name_separator(*byte))
    {
        *position += 1;
    }
    *position > start
}

/// Match GT[ _]?New[ _]?Horizons[ _]+ at the supplied byte offset.
fn gtnh_prefix_end(bytes: &[u8], start: usize) -> Option<usize> {
    if !word_at(bytes, start, b"GT") {
        return None;
    }
    let mut position = start + 2;
    consume_optional_separator(bytes, &mut position);

    if !word_at(bytes, position, b"New") {
        return None;
    }
    position += 3;
    consume_optional_separator(bytes, &mut position);

    if !word_at(bytes, position, b"Horizons") {
        return None;
    }
    position += 8;

    if !consume_required_separators(bytes, &mut position) {
        return None;
    }
    Some(position)
}

/// Match [ _]+Server[ _]+Java[ _]+(8|17-\d+).
///
/// The Python regex does not anchor the Java token at the end of the input:
/// Java_8_suffix still identifies the Java 8 variant. We preserve that rule.
fn java_variant_after_separator(bytes: &[u8], separator_start: usize) -> Option<PackVariant> {
    let mut position = separator_start;
    if !consume_required_separators(bytes, &mut position) || !word_at(bytes, position, b"Server") {
        return None;
    }
    position += 6;

    if !consume_required_separators(bytes, &mut position) || !word_at(bytes, position, b"Java") {
        return None;
    }
    position += 4;

    if !consume_required_separators(bytes, &mut position) {
        return None;
    }

    if bytes.get(position) == Some(&b'8') {
        return Some(PackVariant::Java8);
    }

    if bytes
        .get(position..position.saturating_add(3))
        .is_some_and(|prefix| prefix == b"17-")
    {
        let digits_start = position + 3;
        if bytes.get(digits_start).is_some_and(u8::is_ascii_digit) {
            return Some(PackVariant::Java17);
        }
    }

    None
}

/// Find the first syntactically matching Server Java suffix after a version.
///
/// The source Python regex uses a lazy .+? version capture. Therefore the
/// first delimiter whose remaining suffix matches wins, even when the
/// resulting version later fails parse_pack_version.
fn first_server_suffix<'a>(
    raw: &'a str,
    bytes: &[u8],
    version_start: usize,
) -> Option<(&'a str, PackVariant)> {
    for separator_start in version_start.saturating_add(1)..bytes.len() {
        if !bytes
            .get(separator_start)
            .is_some_and(|byte| is_name_separator(*byte))
        {
            continue;
        }

        // Python's default regex dot does not cross a line feed.
        if bytes
            .get(version_start..separator_start)
            .is_some_and(|version| version.contains(&b'\n'))
        {
            break;
        }

        if let Some(variant) = java_variant_after_separator(bytes, separator_start) {
            // The start and end offsets are ASCII separators, hence valid
            // UTF-8 boundaries. get() keeps that assumption checked.
            let version = raw.get(version_start..separator_start)?;
            return Some((version, variant));
        }
    }
    None
}

/// Parse a GTNH server-pack name into its version and Java variant.
///
/// This mirrors Crucible's identify_gtnh_name() search pattern for ordinary
/// ASCII release names. It searches anywhere in the input (not just the
/// basename), preserves the source version spelling, and accepts trailing
/// text after the Java token. It validates the captured version through
/// parse_pack_version().
///
/// Known Python/Rust parity gaps in parse_pack_version() also apply here:
/// Python accepts Unicode decimal digits and strips U+001C..U+001F, whereas
/// this dependency-free Rust pilot currently accepts ASCII digits and uses
/// Rust's Unicode trim().
pub fn identify_gtnh_name(raw: &str) -> Option<GtnhServerName> {
    let bytes = raw.as_bytes();

    for start in 0..bytes.len().saturating_sub(1) {
        let Some(version_start) = gtnh_prefix_end(bytes, start) else {
            continue;
        };
        let Some((captured_version, variant)) = first_server_suffix(raw, bytes, version_start)
        else {
            continue;
        };
        let version = captured_version.trim();
        if parse_pack_version(version).is_some() {
            return Some(GtnhServerName {
                version: version.to_owned(),
                variant,
            });
        }

        // Python's identify_gtnh_name() uses a single regex search and then
        // validates that one match. It does not continue to a later GTNH
        // substring when the first syntactic match has an invalid version.
        return None;
    }

    None
}

#[cfg(test)]
mod tests {
    use super::{GtnhServerName, PackVariant, identify_gtnh_name};

    #[test]
    fn shared_python_fixture_cases_match() {
        for (line_number, line) in include_str!("../fixtures/pack-name-cases.tsv")
            .lines()
            .enumerate()
        {
            if line.is_empty() || line.starts_with('#') {
                continue;
            }

            let fields: Vec<&str> = line.split('\t').collect();
            assert_eq!(
                fields.len(),
                3,
                "fixture line {} must have three tab-separated fields",
                line_number + 1
            );

            let expected_version = fields[1];
            let expected_variant = fields[2];
            let actual = identify_gtnh_name(fields[0]);

            if expected_version == "-" {
                assert!(
                    actual.is_none(),
                    "line {}: {:?} should not identify",
                    line_number + 1,
                    fields[0]
                );
            } else {
                let GtnhServerName { version, variant } = actual.unwrap_or_else(|| {
                    panic!("line {}: {:?} should identify", line_number + 1, fields[0])
                });
                assert_eq!(
                    version,
                    expected_version,
                    "line {}: {:?}",
                    line_number + 1,
                    fields[0]
                );
                assert_eq!(
                    variant.as_str(),
                    expected_variant,
                    "line {}: {:?}",
                    line_number + 1,
                    fields[0]
                );
            }
        }
    }

    #[test]
    fn invalid_unicode_version_remains_a_documented_python_difference() {
        // Python's \d accepts Arabic-Indic decimal digits in the version.
        assert!(identify_gtnh_name("GT_New_Horizons_٢.٨.٤_Server_Java_8.zip").is_none());
    }

    #[test]
    fn java_variant_is_a_prefix_token_not_an_end_anchored_suffix() {
        assert_eq!(
            identify_gtnh_name("GT_New_Horizons_2.8.4_Server_Java_8_suffix")
                .unwrap()
                .variant,
            PackVariant::Java8
        );
        assert_eq!(
            identify_gtnh_name("GT_New_Horizons_2.8.4_Server_Java_17-25suffix")
                .unwrap()
                .variant,
            PackVariant::Java17
        );
    }
}
