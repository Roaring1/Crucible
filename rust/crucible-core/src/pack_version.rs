use std::cmp::Ordering;

/// A GTNH release channel. The order matches Crucible's Python rank values.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Channel {
    Alpha,
    Beta,
    Rc,
    Stable,
    Unknown,
}

impl Channel {
    /// Return the display name used by the existing Python implementation.
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Alpha => "alpha",
            Self::Beta => "beta",
            Self::Rc => "rc",
            Self::Stable => "stable",
            Self::Unknown => "unknown",
        }
    }

    /// Return the sortable rank used in Python's five-element version tuple.
    pub const fn rank(self) -> Option<u8> {
        match self {
            Self::Alpha => Some(0),
            Self::Beta => Some(1),
            Self::Rc => Some(2),
            Self::Stable => Some(3),
            Self::Unknown => None,
        }
    }
}

/// A non-negative decimal integer stored without leading zeroes.
///
/// Keeping components as decimal strings avoids imposing a fixed-width integer
/// limit that does not exist in the Python tuple implementation.
#[derive(Clone, Debug, Eq, PartialEq)]
struct Decimal(String);

impl Decimal {
    fn zero() -> Self {
        Self("0".to_owned())
    }

    fn from_ascii_digits(value: &str) -> Self {
        let canonical = value.trim_start_matches('0');
        if canonical.is_empty() {
            Self::zero()
        } else {
            Self(canonical.to_owned())
        }
    }
}

impl Ord for Decimal {
    fn cmp(&self, other: &Self) -> Ordering {
        self.0
            .len()
            .cmp(&other.0.len())
            .then_with(|| self.0.as_bytes().cmp(other.0.as_bytes()))
    }
}

impl PartialOrd for Decimal {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

/// Parsed GTNH-style version, ordered like Python's
/// (major, minor, patch, channel_rank, prerelease_number) tuple.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct PackVersion {
    major: Decimal,
    minor: Decimal,
    patch: Decimal,
    channel: Channel,
    prerelease_number: Decimal,
}

impl PackVersion {
    /// Return the version tuple in the stable fixture format.
    pub fn fixture_key(&self) -> String {
        format!(
            "{}.{}.{}|{}|{}",
            self.major.0,
            self.minor.0,
            self.patch.0,
            self.channel
                .rank()
                .expect("parsed versions have a known channel"),
            self.prerelease_number.0
        )
    }

    pub const fn channel(&self) -> Channel {
        self.channel
    }
}

impl Ord for PackVersion {
    fn cmp(&self, other: &Self) -> Ordering {
        self.major
            .cmp(&other.major)
            .then_with(|| self.minor.cmp(&other.minor))
            .then_with(|| self.patch.cmp(&other.patch))
            .then_with(|| self.channel.rank().cmp(&other.channel.rank()))
            .then_with(|| self.prerelease_number.cmp(&other.prerelease_number))
    }
}

impl PartialOrd for PackVersion {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

fn consume_ascii_digits(bytes: &[u8], mut position: usize) -> usize {
    while bytes
        .get(position)
        .is_some_and(|byte| byte.is_ascii_digit())
    {
        position += 1;
    }
    position
}

fn consume_separators(bytes: &[u8], mut position: usize) -> usize {
    while bytes
        .get(position)
        .is_some_and(|byte| matches!(*byte, b'-' | b'_' | b'.' | b' '))
    {
        position += 1;
    }
    position
}

fn decimal_from_range(input: &str, start: usize, end: usize) -> Option<Decimal> {
    input.get(start..end).map(Decimal::from_ascii_digits)
}

/// Parse the ASCII form of a GTNH release version.
///
/// Accepted forms mirror the current Python expression for ASCII input:
/// 2.8.4, v2.8, 2.9.0-beta-3, 2.5.0-RC1, and 2.9.0 beta 3.
/// The Python regex also accepts every Unicode decimal digit class; this first
/// pilot intentionally accepts ASCII digits only. It is not called by
/// production code. Python remains authoritative until this edge is handled.
pub fn parse_pack_version(raw: &str) -> Option<PackVersion> {
    let trimmed = raw.trim();
    let value = trimmed
        .strip_prefix('v')
        .or_else(|| trimmed.strip_prefix('V'))
        .unwrap_or(trimmed);
    let bytes = value.as_bytes();

    let major_start = 0;
    let major_end = consume_ascii_digits(bytes, major_start);
    if major_end == major_start || bytes.get(major_end) != Some(&b'.') {
        return None;
    }

    let minor_start = major_end + 1;
    let minor_end = consume_ascii_digits(bytes, minor_start);
    if minor_end == minor_start {
        return None;
    }

    let mut position = minor_end;
    let mut patch = Decimal::zero();

    // A dot is a patch separator only when a digit follows it. Otherwise it
    // may be one of the allowed separators before a prerelease channel.
    if bytes.get(position) == Some(&b'.')
        && bytes
            .get(position + 1)
            .is_some_and(|byte| byte.is_ascii_digit())
    {
        let patch_start = position + 1;
        let patch_end = consume_ascii_digits(bytes, patch_start);
        patch = decimal_from_range(value, patch_start, patch_end)?;
        position = patch_end;
    }

    let (channel, prerelease_number) = if position == bytes.len() {
        (Channel::Stable, Decimal::zero())
    } else {
        let channel_start = consume_separators(bytes, position);
        let remainder = bytes.get(channel_start..)?;
        let (channel, channel_len) = if remainder
            .get(..5)
            .is_some_and(|word| word.eq_ignore_ascii_case(b"alpha"))
        {
            (Channel::Alpha, 5)
        } else if remainder
            .get(..4)
            .is_some_and(|word| word.eq_ignore_ascii_case(b"beta"))
        {
            (Channel::Beta, 4)
        } else if remainder
            .get(..2)
            .is_some_and(|word| word.eq_ignore_ascii_case(b"rc"))
        {
            (Channel::Rc, 2)
        } else {
            return None;
        };

        position = channel_start + channel_len;
        position = consume_separators(bytes, position);
        let number_start = position;
        position = consume_ascii_digits(bytes, position);
        if position != bytes.len() {
            return None;
        }
        let number = if number_start == position {
            Decimal::zero()
        } else {
            decimal_from_range(value, number_start, position)?
        };
        (channel, number)
    };

    Some(PackVersion {
        major: decimal_from_range(value, major_start, major_end)?,
        minor: decimal_from_range(value, minor_start, minor_end)?,
        patch,
        channel,
        prerelease_number,
    })
}

/// Return the parsed channel, or Unknown when the version is invalid.
pub fn channel_of(raw: &str) -> Channel {
    parse_pack_version(raw)
        .map(|version| version.channel())
        .unwrap_or(Channel::Unknown)
}

#[cfg(test)]
mod tests {
    use super::{Channel, channel_of, parse_pack_version};

    #[test]
    fn shared_python_fixture_cases_match() {
        for (line_number, line) in include_str!("../fixtures/pack-version-cases.tsv")
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

            let input = fields[0];
            let expected_key = fields[1];
            let expected_channel = fields[2];
            let actual = parse_pack_version(input);

            if expected_key == "-" {
                assert!(
                    actual.is_none(),
                    "line {}: {:?} should be invalid",
                    line_number + 1,
                    input
                );
            } else {
                let actual = actual.unwrap_or_else(|| {
                    panic!("line {}: {:?} should parse", line_number + 1, input)
                });
                assert_eq!(
                    actual.fixture_key(),
                    expected_key,
                    "line {}: {:?}",
                    line_number + 1,
                    input
                );
            }

            assert_eq!(
                channel_of(input).as_str(),
                expected_channel,
                "line {}: {:?}",
                line_number + 1,
                input
            );
        }
    }

    #[test]
    fn prereleases_sort_below_the_matching_stable_release() {
        let beta = parse_pack_version("2.9.0-beta-3").unwrap();
        let release_candidate = parse_pack_version("2.9.0-rc-1").unwrap();
        let stable = parse_pack_version("2.9.0").unwrap();

        assert!(beta < release_candidate);
        assert!(release_candidate < stable);
    }

    #[test]
    fn channel_and_prerelease_order_matches_python_tuple_semantics() {
        let alpha = parse_pack_version("2.9.0-alpha-10").unwrap();
        let beta = parse_pack_version("2.9.0-beta-0").unwrap();
        let beta_two = parse_pack_version("2.9.0-beta-2").unwrap();
        let beta_ten = parse_pack_version("2.9.0-beta-10").unwrap();
        let rc = parse_pack_version("2.9.0-rc-1").unwrap();
        let stable = parse_pack_version("2.9.0").unwrap();

        assert!(alpha < beta);
        assert!(beta < beta_two);
        assert!(beta_two < beta_ten);
        assert!(beta_ten < rc);
        assert!(rc < stable);
        assert_eq!(
            parse_pack_version("2.9").unwrap(),
            parse_pack_version("2.9.0").unwrap()
        );
    }

    #[test]
    fn arbitrary_length_components_do_not_overflow() {
        let larger = parse_pack_version("123456789012345678901234567890.1.0").unwrap();
        let smaller = parse_pack_version("99999999999999999999999999999.1.0").unwrap();
        assert!(larger > smaller);
    }

    #[test]
    fn known_python_parity_differences_remain_explicit() {
        // Python's \d and int() accept Unicode decimal digits. Rust's
        // dependency-free pilot currently accepts ASCII digits only.
        assert!(parse_pack_version("٢.٨.٤").is_none());

        // Python str.strip() also strips U+001C..U+001F; Rust str::trim()
        // follows Unicode White_Space and does not strip these separators.
        assert!(parse_pack_version("\u{001c}2.8.4\u{001c}").is_none());
    }

    #[test]
    fn invalid_versions_have_unknown_channel() {
        assert_eq!(channel_of("April fools 2025"), Channel::Unknown);
        assert_eq!(channel_of("2.8.4-preview-1"), Channel::Unknown);
    }
}
