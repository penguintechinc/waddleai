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
        save_to_entry(&self.entry()?.inner, tokens)
    }

    fn load(&self) -> Result<Option<StoredTokens>> {
        load_from_entry(&self.entry()?.inner)
    }

    fn clear(&self) -> Result<()> {
        clear_entry(&self.entry()?.inner)
    }
}

/// Serializes `tokens` as JSON into the credential `entry`. Split out from
/// the trait impl so the logic is testable against a mock credential store.
fn save_to_entry(entry: &keyring_core::Entry, tokens: &StoredTokens) -> Result<()> {
    let serialized = serde_json::to_string(tokens)?;
    entry.set_password(&serialized)?;
    Ok(())
}

/// Reads and decodes the session from `entry`; a missing entry is `None`,
/// an undecodable payload is `InvalidSession` (never a panic).
fn load_from_entry(entry: &keyring_core::Entry) -> Result<Option<StoredTokens>> {
    match entry.get_password() {
        Ok(raw) => {
            let tokens: StoredTokens =
                serde_json::from_str(&raw).map_err(|_| CliError::InvalidSession)?;
            Ok(Some(tokens))
        }
        Err(keyring::Error::NoEntry) => Ok(None),
        Err(err) => Err(err.into()),
    }
}

/// Deletes the credential; deleting an already-absent entry is success.
fn clear_entry(entry: &keyring_core::Entry) -> Result<()> {
    match entry.delete_credential() {
        Ok(()) | Err(keyring::Error::NoEntry) => Ok(()),
        Err(err) => Err(err.into()),
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
    use keyring_core::api::CredentialStoreApi;

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

    fn mock_entry(user: &str) -> keyring_core::Entry {
        let store = keyring_core::mock::Store::new().expect("mock store");
        store.build(SERVICE_NAME, user, None).expect("mock entry")
    }

    fn inject_error(entry: &keyring_core::Entry) {
        entry
            .as_any()
            .downcast_ref::<keyring_core::mock::Cred>()
            .expect("mock cred")
            .set_error(keyring_core::Error::NoStorageAccess("locked".into()));
    }

    #[test]
    fn entry_round_trips_tokens_as_json() {
        let entry = mock_entry("rt");
        assert!(load_from_entry(&entry).expect("load").is_none());
        save_to_entry(&entry, &sample_tokens()).expect("save");
        assert_eq!(
            load_from_entry(&entry).expect("load").expect("some"),
            sample_tokens()
        );
        clear_entry(&entry).expect("clear");
        assert!(load_from_entry(&entry).expect("load").is_none());
    }

    #[test]
    fn clear_on_absent_entry_is_ok() {
        clear_entry(&mock_entry("absent")).expect("idempotent clear");
    }

    #[test]
    fn corrupt_payload_is_invalid_session_not_panic() {
        let entry = mock_entry("corrupt");
        entry.set_password("{not json").expect("set");
        assert!(matches!(
            load_from_entry(&entry),
            Err(CliError::InvalidSession)
        ));
    }

    #[test]
    fn store_backend_errors_propagate_as_keyring_errors() {
        let entry = mock_entry("errs");
        inject_error(&entry);
        assert!(matches!(
            save_to_entry(&entry, &sample_tokens()),
            Err(CliError::Keyring(_))
        ));
        inject_error(&entry);
        assert!(matches!(load_from_entry(&entry), Err(CliError::Keyring(_))));
        inject_error(&entry);
        assert!(matches!(clear_entry(&entry), Err(CliError::Keyring(_))));
    }
}
