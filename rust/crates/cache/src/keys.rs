//! Valkey key builders, byte-identical to the Python proxy
//! (`shared/cache/*`, `shared/auth/rbac.py`, `shared/quota/keys.py`).
//! Each fn cites its Python source; change both sides together.

/// `shared/cache/exact.py::_entry_key` — one cached response.
pub fn exact_entry(org_id: i64, key: &str) -> String {
    format!("waddleai:cache:exact:{org_id}:{key}")
}

/// `shared/cache/exact.py::_idx_key` — per-org LRU index.
pub fn exact_idx(org_id: i64) -> String {
    format!("waddleai:cache:idx:{org_id}")
}

/// `shared/cache/exact.py::_bytes_key` — per-org byte counter.
pub fn exact_bytes(org_id: i64) -> String {
    format!("waddleai:cache:bytes:{org_id}")
}

/// `shared/cache/response_cache.py::_exact_lease_key` — stampede lease base key.
pub fn exact_lease(org_id: i64, key: &str) -> String {
    format!("waddleai:cache:sf:exact:{org_id}:{key}")
}

/// `shared/cache/response_cache.py::_semantic_lease_key`.
pub fn semantic_lease(org_id: i64, model_class: &str, context_hash: &str) -> String {
    format!("waddleai:cache:sf:semantic:{org_id}:{model_class}:{context_hash}")
}

/// `shared/cache/singleflight.py::_lock_key` — the lock key for a lease base key.
pub fn lock(cache_key: &str) -> String {
    format!("{cache_key}:lock")
}

/// `shared/cache/config.py::scope_cache_key`; `None` scope_ref renders `-`.
pub fn scope_cache(scope_type: &str, scope_ref: Option<&str>) -> String {
    format!(
        "waddleai:cache:cfg:{scope_type}:{}",
        scope_ref.unwrap_or("-")
    )
}

/// `shared/cache/affinity.py::_key`.
pub fn affinity(org_id: i64, session_hash: &str) -> String {
    format!("waddleai:affinity:{org_id}:{session_hash}")
}

/// `shared/cache/upstream.py::_mapping_key` — Gemini cached-content mapping.
pub fn gemini_mapping(vkey_id: i64, prefix_sha: &str) -> String {
    format!("waddleai:cache:gemini:{vkey_id}:{prefix_sha}")
}

/// `shared/cache/upstream.py` — prefix observation counter.
pub fn prefix_counter(vkey_id: i64, prefix_sha: &str) -> String {
    format!("waddleai:cache:prefix:{vkey_id}:{prefix_sha}")
}

/// `shared/auth/rbac.py::AUTH_CACHE_KEY_PREFIX + key_id` — API-key auth cache.
pub fn auth_apikey(key_id: &str) -> String {
    format!("waddleai:auth:apikey:{key_id}")
}

/// Prefix of every ContentFilter org-rules cache key. Rust-side only: the Python
/// filter caches rules in-process and has no Valkey key, so there is no Python twin.
pub const CONTENT_FILTER_RULES_PREFIX: &str = "waddleai:cf:rules:";

/// ContentFilter tier-2 rules + per-org toggles for one org; `None` is the global scope.
pub fn content_filter_rules(org_id: Option<i64>) -> String {
    match org_id {
        Some(id) => format!("{CONTENT_FILTER_RULES_PREFIX}{id}"),
        None => format!("{CONTENT_FILTER_RULES_PREFIX}global"),
    }
}

/// `shared/quota/keys.py` — pooled-quota counters and config caches.
pub mod quota {
    /// `PREFIX`.
    pub const PREFIX: &str = "wa:quota";
    /// `RESERVATION_INDEX`.
    pub const RESERVATION_INDEX: &str = "wa:quota:resv_idx";

    /// `user_counter`.
    pub fn user_counter(user_id: i64, cycle: i64) -> String {
        format!("wa:quota:user:{user_id}:{cycle}")
    }
    /// `buffer_counter`.
    pub fn buffer_counter(team_id: i64, cycle: i64) -> String {
        format!("wa:quota:team_buffer:{team_id}:{cycle}")
    }
    /// `override_counter` (`OVERRIDE_PREFIX` = `wa:quota:override:`).
    pub fn override_counter(override_id: i64, cycle: i64) -> String {
        format!("wa:quota:override:{override_id}:{cycle}")
    }
    /// `daily_counter`; `day` is `YYYYMMDD`.
    pub fn daily_counter(user_id: i64, day: &str) -> String {
        format!("wa:quota:userday:{user_id}:{day}")
    }
    /// `reservation`.
    pub fn reservation(reservation_id: &str) -> String {
        format!("wa:quota:resv:{reservation_id}")
    }
    /// `org_config`.
    pub fn org_config(org_id: i64) -> String {
        format!("wa:quota:cfg:org:{org_id}")
    }
    /// `team_config`.
    pub fn team_config(team_id: i64) -> String {
        format!("wa:quota:cfg:team:{team_id}")
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Expected strings copied verbatim from the Python proxy; runs without Valkey.
    #[test]
    fn key_formats_match_python() {
        // shared/cache/exact.py
        assert_eq!(exact_entry(7, "abc"), "waddleai:cache:exact:7:abc");
        assert_eq!(exact_idx(7), "waddleai:cache:idx:7");
        assert_eq!(exact_bytes(7), "waddleai:cache:bytes:7");
        // shared/cache/response_cache.py + singleflight.py
        assert_eq!(exact_lease(7, "abc"), "waddleai:cache:sf:exact:7:abc");
        assert_eq!(
            semantic_lease(7, "gpt-4o", "h1"),
            "waddleai:cache:sf:semantic:7:gpt-4o:h1"
        );
        assert_eq!(
            lock("waddleai:cache:sf:exact:7:abc"),
            "waddleai:cache:sf:exact:7:abc:lock"
        );
        // shared/cache/config.py
        assert_eq!(scope_cache("org", Some("5")), "waddleai:cache:cfg:org:5");
        assert_eq!(scope_cache("global", None), "waddleai:cache:cfg:global:-");
        // shared/cache/affinity.py, upstream.py
        assert_eq!(affinity(7, "sess"), "waddleai:affinity:7:sess");
        assert_eq!(gemini_mapping(3, "sha"), "waddleai:cache:gemini:3:sha");
        assert_eq!(prefix_counter(3, "sha"), "waddleai:cache:prefix:3:sha");
        // shared/auth/rbac.py
        assert_eq!(auth_apikey("kid"), "waddleai:auth:apikey:kid");
        // Rust-side only (no Python twin): content-filter rules cache
        assert_eq!(content_filter_rules(Some(7)), "waddleai:cf:rules:7");
        assert_eq!(content_filter_rules(None), "waddleai:cf:rules:global");
        // shared/quota/keys.py
        assert_eq!(quota::user_counter(1, 2), "wa:quota:user:1:2");
        assert_eq!(quota::buffer_counter(1, 2), "wa:quota:team_buffer:1:2");
        assert_eq!(quota::override_counter(1, 2), "wa:quota:override:1:2");
        assert_eq!(
            quota::daily_counter(1, "20261028"),
            "wa:quota:userday:1:20261028"
        );
        assert_eq!(quota::reservation("r1"), "wa:quota:resv:r1");
        assert_eq!(quota::org_config(9), "wa:quota:cfg:org:9");
        assert_eq!(quota::team_config(9), "wa:quota:cfg:team:9");
        assert_eq!(quota::RESERVATION_INDEX, "wa:quota:resv_idx");
        assert_eq!(quota::PREFIX, "wa:quota");
    }
}
