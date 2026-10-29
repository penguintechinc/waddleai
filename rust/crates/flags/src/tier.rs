//! License tiers (cumulative): `free` < `professional` < `enterprise`.

use std::fmt;
use std::str::FromStr;

/// A license tier. Ordering is cumulative, so `Enterprise >= Professional`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum Tier {
    /// Unmetered baseline.
    Free,
    /// Professional tier.
    Professional,
    /// Enterprise tier.
    Enterprise,
}

/// An unrecognised tier string.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("unknown license tier")]
pub struct TierParseError;

impl Tier {
    /// Lowercase wire name.
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Free => "free",
            Self::Professional => "professional",
            Self::Enterprise => "enterprise",
        }
    }
}

impl fmt::Display for Tier {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

impl FromStr for Tier {
    type Err = TierParseError;
    fn from_str(s: &str) -> Result<Self, Self::Err> {
        match s.to_ascii_lowercase().as_str() {
            "free" => Ok(Self::Free),
            "professional" => Ok(Self::Professional),
            "enterprise" => Ok(Self::Enterprise),
            _ => Err(TierParseError),
        }
    }
}
