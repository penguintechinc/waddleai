//! Token persistence -- OS-native credential store only.
//!
//! Per client standards, a distributed client never writes tokens to a
//! plaintext file. `KeyringTokenStore` is the only production
//! implementation, backed by macOS Keychain / Windows Credential Manager /
//! Linux Secret Service via the `keyring` crate. `TokenStore` is a trait so
//! tests can substitute an in-memory fake instead of depending on a live
//! OS credential store being available in CI.

use serde::{Deserialize, Serialize};

use crate::error::{CliError, Result};

const SERVICE_NAME: &str = "waddleai-cli";
const ACCOUNT_NAME: &str = "default";

/// Tokens for one logged-in session. `expires_at` is a Unix epoch (seconds).
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct StoredTokens {
    pub access_token: String,
    pub refresh_token: Option<String>,
    pub expires_at: i64,
    pub api_url: String,
}

impl StoredTokens {
    /// True once `expires_at` is at or before now (with no grace window --
    /// callers needing a refresh-ahead margin apply it themselves).
    pub fn is_expired(&self, now_epoch: i64) -> bool {
        now_epoch >= self.expires_at
    }
}

/// Redacts a token to its first/last few characters for safe display --
/// e.g. in `waddleai keys` verbose output. Never used for the raw value.
pub fn mask_token(token: &str) -> String {
    let len = token.chars().count();
    if len <= 8 {
        return "****".to_string();
    }
    let prefix: String = token.chars().take(4).collect();
    let suffix: String = token.chars().skip(len - 4).collect();
    format!("{prefix}...{suffix}")
}

/// Abstraction over "wherever the tokens are stored" so commands don't
/// depend directly on the `keyring` crate (and so tests don't depend on a
/// live OS credential store).
pub trait TokenStore {
    fn save(&self, tokens: &StoredTokens) -> Result<()>;
    fn load(&self) -> Result<Option<StoredTokens>>;
    fn clear(&self) -> Result<()>;
}

/// Production `TokenStore` backed by the OS-native credential store.
pub struct KeyringTokenStore;

impl KeyringTokenStore {
    fn entry(&self) -> Result<keyring::Entry> {
        Ok(keyring::Entry::new(SERVICE_NAME, ACCOUNT_NAME)?)
    }
}

impl TokenStore for KeyringTokenStore {
    fn save(&self, tokens: &StoredTokens) -> Result<()> {
        let serialized = serde_json::to_string(tokens)?;
        self.entry()?.set_password(&serialized)?;
        Ok(())
    }

    fn load(&self) -> Result<Option<StoredTokens>> {
        match self.entry()?.get_password() {
            Ok(raw) => {
                let tokens: StoredTokens =
                    serde_json::from_str(&raw).map_err(|_| CliError::InvalidSession)?;
                Ok(Some(tokens))
            }
            Err(keyring::Error::NoEntry) => Ok(None),
            Err(err) => Err(err.into()),
        }
    }

    fn clear(&self) -> Result<()> {
        match self.entry()?.delete_credential() {
            Ok(()) | Err(keyring::Error::NoEntry) => Ok(()),
            Err(err) => Err(err.into()),
        }
    }
}

#[cfg(test)]
pub mod test_support {
    //! In-memory `TokenStore` for tests -- never touches a real OS
    //! credential store, so CLI tests are hermetic in CI environments
    //! without a Secret Service / Keychain / Credential Manager available.
    use std::sync::Mutex;

    use super::*;

    #[derive(Default)]
    pub struct InMemoryTokenStore {
        slot: Mutex<Option<StoredTokens>>,
    }

    impl TokenStore for InMemoryTokenStore {
        fn save(&self, tokens: &StoredTokens) -> Result<()> {
            *self.slot.lock().expect("test mutex poisoned") = Some(tokens.clone());
            Ok(())
        }

        fn load(&self) -> Result<Option<StoredTokens>> {
            Ok(self.slot.lock().expect("test mutex poisoned").clone())
        }

        fn clear(&self) -> Result<()> {
            *self.slot.lock().expect("test mutex poisoned") = None;
            Ok(())
        }
    }
}

#[cfg(test)]
mod tests {
    use super::test_support::InMemoryTokenStore;
    use super::*;

    fn sample_tokens() -> StoredTokens {
        StoredTokens {
            access_token: "at-secret-value".into(),
            refresh_token: Some("rt-secret-value".into()),
            expires_at: 1_000,
            api_url: "https://waddleai.example".into(),
        }
    }

    #[test]
    fn in_memory_store_round_trips() {
        let store = InMemoryTokenStore::default();
        assert!(store.load().expect("load").is_none());

        store.save(&sample_tokens()).expect("save");
        let loaded = store.load().expect("load").expect("some");
        assert_eq!(loaded, sample_tokens());

        store.clear().expect("clear");
        assert!(store.load().expect("load").is_none());
    }

    #[test]
    fn is_expired_true_at_or_after_expiry() {
        let tokens = sample_tokens();
        assert!(!tokens.is_expired(999));
        assert!(tokens.is_expired(1_000));
        assert!(tokens.is_expired(1_001));
    }

    #[test]
    fn mask_token_never_exposes_middle() {
        assert_eq!(mask_token("wa-abcdefghijklmnop"), "wa-a...mnop");
        assert_eq!(mask_token("short"), "****");
    }
}
