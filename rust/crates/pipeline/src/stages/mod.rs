//! Concrete pipeline stages that live in the `pipeline` crate.

pub mod auth;
pub mod auth_mode;

pub use auth::{
    AuthStage, Authenticator, DenyPassthrough, KeyLimitsSource, NoKeyLimits, PassthroughPolicy,
    RequestHeaders,
};
pub use auth_mode::{detect_auth_mode, CredentialKind, PROVIDER_KEY_HEADER};
