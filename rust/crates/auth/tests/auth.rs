//! Auth tests. The `wa_key_*` tests need `TEST_DATABASE_URL` (a Postgres with the
//! Alembic schema applied) and FAIL loudly when it is unset -- never skipped.
use std::time::{SystemTime, UNIX_EPOCH};

use auth::{
    authenticate_api_key, verify_jwt, AuthError, AuthMode, Claims, Jwks, JwtPolicy, UserContext,
};
use aws_lc_rs::encoding::AsDer;
use aws_lc_rs::rsa::{KeyPair, KeySize};
use aws_lc_rs::signature::KeyPair as _;
use base64::Engine;
use db::Connection;
use sea_orm::{ConnectionTrait, DbBackend, Statement};
use serde_json::json;
use tokio::sync::OnceCell;

/// Minted by the Python path (`shared.auth.rbac.mint_wa_credential` +
/// `passlib.hash.bcrypt.hash`); its secret contains `-` on purpose. Test-only
/// credential for an ephemeral database -- never valid anywhere real.
const PY_KEY_ID: &str = "5d5ea1c7e2f7ecf8";
const PY_KEY: &str = "wa-5d5ea1c7e2f7ecf8-Kot_hM56E-eqnZoHJh1K_1d6Y8hXmm73M7Aj5ycgAh8"; // nosec
const PY_HASH: &str = "$2b$12$xfUQ2oJI.aVmMGjpWo7hMe6w4tTmqdgyqYsWiSTvgK8gn.dTJTbK2"; // nosec

/// A fresh pool per test: each `#[tokio::test]` has its own runtime, and a pool shared
/// across runtimes dies with the runtime that created it.
async fn conn() -> Connection {
    let url = std::env::var("TEST_DATABASE_URL")
        .expect("TEST_DATABASE_URL must point at a migrated Postgres");
    let opts = db::DbOptions {
        pool_size: 2,
        ..db::DbOptions::default()
    };
    db::connect_with(&url, &opts).await.expect("connect")
}

async fn exec(c: &Connection, sql: &str, vals: Vec<sea_orm::Value>) {
    c.execute_raw(Statement::from_sql_and_values(
        DbBackend::Postgres,
        sql,
        vals,
    ))
    .await
    .expect("seed sql");
}

async fn scalar_i32(c: &Connection, sql: &str, vals: Vec<sea_orm::Value>) -> i32 {
    let row = c
        .query_one_raw(Statement::from_sql_and_values(
            DbBackend::Postgres,
            sql,
            vals,
        ))
        .await
        .expect("query")
        .expect("row");
    row.try_get("", "id").expect("id")
}

struct Seeded {
    org_id: i32,
    user_id: i32,
}

/// Idempotent org + user seed, keyed by a unique tag so tests never collide.
async fn seed_user(c: &Connection, tag: &str, role: &str, enabled: bool) -> Seeded {
    exec(
        c,
        "INSERT INTO organizations (name) VALUES ($1) ON CONFLICT DO NOTHING",
        vec![format!("auth-t-{tag}").into()],
    )
    .await;
    let org_id = scalar_i32(
        c,
        "SELECT id FROM organizations WHERE name = $1",
        vec![format!("auth-t-{tag}").into()],
    )
    .await;
    exec(
        c,
        "DELETE FROM api_keys WHERE user_id IN (SELECT id FROM users WHERE username = $1)",
        vec![format!("u-{tag}").into()],
    )
    .await;
    exec(
        c,
        "DELETE FROM team_members WHERE user_id IN (SELECT id FROM users WHERE username = $1)",
        vec![format!("u-{tag}").into()],
    )
    .await;
    exec(
        c,
        "DELETE FROM users WHERE username = $1",
        vec![format!("u-{tag}").into()],
    )
    .await;
    exec(
        c,
        "INSERT INTO users (username, email, password_hash, role, organization_id, enabled, managed_orgs) \
         VALUES ($1, $2, 'x', $3, $4, $5, '[7, 9]'::json)",
        vec![
            format!("u-{tag}").into(),
            format!("u-{tag}@example.test").into(),
            role.into(),
            org_id.into(),
            enabled.into(),
        ],
    )
    .await;
    let user_id = scalar_i32(
        c,
        "SELECT id FROM users WHERE username = $1",
        vec![format!("u-{tag}").into()],
    )
    .await;
    Seeded { org_id, user_id }
}

async fn seed_key(
    c: &Connection,
    s: &Seeded,
    key_id: &str,
    hash: &str,
    enabled: bool,
    expires: Option<&str>,
    team_id: Option<i32>,
) {
    exec(
        c,
        "DELETE FROM api_keys WHERE key_id = $1",
        vec![key_id.into()],
    )
    .await;
    exec(
        c,
        "INSERT INTO api_keys (key_id, key_hash, user_id, organization_id, name, enabled, expires_at, team_id) \
         VALUES ($1, $2, $3, $4, 'auth-test', $5, $6::timestamp, $7)",
        vec![
            key_id.into(),
            hash.into(),
            s.user_id.into(),
            s.org_id.into(),
            enabled.into(),
            expires.map(str::to_owned).into(),
            team_id.into(),
        ],
    )
    .await;
}

fn mint(key_id: &str, secret: &str) -> (String, String) {
    let key = format!("wa-{key_id}-{secret}");
    let hash = bcrypt::hash_with_result(&key, 4).expect("hash").to_string();
    (key, hash)
}

static PY_SEED: OnceCell<Seeded> = OnceCell::const_new();

/// Seed the Python-minted key exactly once per process (tests run in parallel).
async fn seed_py_key(c: &Connection) -> &'static Seeded {
    PY_SEED
        .get_or_init(|| async {
            let s = seed_user(c, "valid", "resource_manager", true).await;
            seed_key(c, &s, PY_KEY_ID, PY_HASH, true, None, None).await;
            s
        })
        .await
}

#[tokio::test]
async fn wa_key_valid_returns_context() {
    let c = &conn().await;
    let s = seed_py_key(c).await;
    let ctx = authenticate_api_key(c, PY_KEY, true)
        .await
        .expect("authenticates");
    assert_eq!(ctx.user_id, s.user_id);
    assert_eq!(ctx.org_id, s.org_id);
    assert_eq!(ctx.tenant, s.org_id.to_string());
    assert_eq!(ctx.auth_mode, AuthMode::ApiKey);
    assert_eq!(ctx.teams, vec![7, 9]);
    assert!(ctx.scopes.contains("proxy:use"));
    assert!(ctx.scopes.contains("quota:update"));
    assert!(
        !ctx.scopes.contains("apikey:admin"),
        "resource_manager is not admin"
    );
    assert!(ctx.api_key_id.is_some());
    assert_eq!(ctx.team_id, None);
}

#[tokio::test]
async fn wa_key_wrong_secret_rejects() {
    let c = &conn().await;
    seed_py_key(c).await;
    let bad = format!("wa-{PY_KEY_ID}-not-the-secret");
    assert!(matches!(
        authenticate_api_key(c, &bad, true).await,
        Err(AuthError::InvalidApiKey)
    ));
}

#[tokio::test]
async fn wa_key_malformed_rejects() {
    let c = &conn().await;
    for bad in [
        "",
        "wa",
        "wa-",
        "wa--",
        "wa-abc",
        "wa-abc-",
        "wa--secret",
        "xx-abc-secret",
        "nodashes",
        "WA-abc-secret",
        "wa-\u{0}-x",
        "wa-ä-ö",
    ] {
        let r = authenticate_api_key(c, bad, true).await;
        assert!(
            matches!(r, Err(AuthError::InvalidApiKey)),
            "{bad:?} -> {r:?}"
        );
    }
}

#[tokio::test]
async fn wa_key_unknown_id_rejects() {
    let c = &conn().await;
    let r = authenticate_api_key(c, "wa-0000000000000000-whatever-secret", true).await;
    assert!(matches!(r, Err(AuthError::InvalidApiKey)));
}

#[tokio::test]
async fn wa_key_disabled_expired_and_disabled_user_reject() {
    let c = &conn().await;
    let s = seed_user(c, "states", "user", true).await;
    let (k1, h1) = mint("aa00000000000001", "s1");
    seed_key(c, &s, "aa00000000000001", &h1, false, None, None).await;
    assert!(matches!(
        authenticate_api_key(c, &k1, true).await,
        Err(AuthError::InvalidApiKey)
    ));

    let (k2, h2) = mint("aa00000000000002", "s2");
    seed_key(
        c,
        &s,
        "aa00000000000002",
        &h2,
        true,
        Some("2000-01-01 00:00:00"),
        None,
    )
    .await;
    assert!(matches!(
        authenticate_api_key(c, &k2, true).await,
        Err(AuthError::InvalidApiKey)
    ));

    let (k3, h3) = mint("aa00000000000003", "s3");
    seed_key(
        c,
        &s,
        "aa00000000000003",
        &h3,
        true,
        Some("2999-01-01 00:00:00"),
        None,
    )
    .await;
    assert!(
        authenticate_api_key(c, &k3, true).await.is_ok(),
        "future expiry is valid"
    );

    let d = seed_user(c, "disabled-user", "user", false).await;
    let (k4, h4) = mint("aa00000000000004", "s4");
    seed_key(c, &d, "aa00000000000004", &h4, true, None, None).await;
    assert!(matches!(
        authenticate_api_key(c, &k4, true).await,
        Err(AuthError::UserDisabled)
    ));
}

#[tokio::test]
async fn wa_key_unknown_role_fails_closed() {
    let c = &conn().await;
    let s = seed_user(c, "badrole", "superuser", true).await;
    let (k, h) = mint("aa00000000000005", "s5");
    seed_key(c, &s, "aa00000000000005", &h, true, None, None).await;
    assert!(matches!(
        authenticate_api_key(c, &k, true).await,
        Err(AuthError::UnknownRole)
    ));
}

#[tokio::test]
async fn wa_key_team_scopes_from_live_membership() {
    let c = &conn().await;
    let s = seed_user(c, "team", "user", true).await;
    exec(
        c,
        "DELETE FROM teams WHERE organization_id = $1 AND name = 'auth-team'",
        vec![s.org_id.into()],
    )
    .await;
    exec(
        c,
        "INSERT INTO teams (organization_id, name, enabled) VALUES ($1, 'auth-team', true)",
        vec![s.org_id.into()],
    )
    .await;
    let team_id = scalar_i32(
        c,
        "SELECT id FROM teams WHERE organization_id = $1 AND name = 'auth-team'",
        vec![s.org_id.into()],
    )
    .await;
    exec(c, "INSERT INTO team_members (team_id, user_id, organization_id, team_role) VALUES ($1, $2, $3, 'admin')",
        vec![team_id.into(), s.user_id.into(), s.org_id.into()]).await;
    let (k, h) = mint("aa00000000000006", "s6");
    seed_key(c, &s, "aa00000000000006", &h, true, None, Some(team_id)).await;
    let ctx = authenticate_api_key(c, &k, true).await.expect("ok");
    assert_eq!(ctx.team_id, Some(team_id));
    assert_eq!(ctx.team_role.as_deref(), Some("admin"));
    assert!(ctx.scopes.contains("team:admin"));
    assert!(!ctx.scopes.contains("team:delete"), "admin < owner");

    // A disabled team resolves teamless -- never an error, never team scopes.
    exec(
        c,
        "UPDATE teams SET enabled = false WHERE id = $1",
        vec![team_id.into()],
    )
    .await;
    let ctx = authenticate_api_key(c, &k, true).await.expect("ok");
    assert_eq!(ctx.team_id, None);
    assert!(!ctx.scopes.contains("team:admin"));
    exec(
        c,
        "DELETE FROM team_members WHERE team_id = $1",
        vec![team_id.into()],
    )
    .await;
    exec(
        c,
        "DELETE FROM api_keys WHERE team_id = $1",
        vec![team_id.into()],
    )
    .await;
    exec(c, "DELETE FROM teams WHERE id = $1", vec![team_id.into()]).await;
}

// ---- JWT ----

struct TestKey {
    der: Vec<u8>,
    jwk: serde_json::Value,
}

fn test_key(kid: &str) -> TestKey {
    let kp = KeyPair::generate(KeySize::Rsa2048).expect("keygen");
    let b64 = base64::engine::general_purpose::URL_SAFE_NO_PAD;
    let public = kp.public_key();
    let jwk = json!({
        "kty": "RSA", "alg": "RS256", "use": "sig", "kid": kid,
        "n": b64.encode(public.modulus().big_endian_without_leading_zero()),
        "e": b64.encode(public.exponent().big_endian_without_leading_zero()),
    });
    let der = AsDer::<aws_lc_rs::encoding::Pkcs8V1Der>::as_der(&kp).expect("der");
    // jsonwebtoken wants PKCS#1 RSAPrivateKey DER; aws-lc exports PKCS#8. For RSA-2048 the
    // PKCS#8 wrapper is a fixed 26-byte header (SEQ, version 0, rsaEncryption AlgId, OCTET STRING).
    const PKCS8_RSA_HEADER_TAIL: [u8; 20] = [
        0x02, 0x01, 0x00, 0x30, 0x0d, 0x06, 0x09, 0x2a, 0x86, 0x48, 0x86, 0xf7, 0x0d, 0x01, 0x01,
        0x01, 0x05, 0x00, 0x04, 0x82,
    ];
    let bytes = der.as_ref();
    assert_eq!(&bytes[..2], &[0x30, 0x82]);
    assert_eq!(
        &bytes[4..24],
        &PKCS8_RSA_HEADER_TAIL,
        "unexpected PKCS#8 layout"
    );
    TestKey {
        der: bytes[26..].to_vec(),
        jwk,
    }
}

fn now() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("clock")
        .as_secs()
}

const ISS: &str = "https://waddleai.test";
const AUD: &str = "waddleai-api";

fn policy() -> JwtPolicy {
    JwtPolicy::new(ISS, AUD)
}

fn good_claims() -> serde_json::Value {
    json!({
        "sub": "42", "iss": ISS, "aud": AUD, "iat": now(), "exp": now() + 600,
        "scope": ["proxy:use", "quota:read"], "tenant": "5", "teams": ["7"], "roles": ["user"],
    })
}

fn sign(
    k: &TestKey,
    kid: &str,
    alg: jsonwebtoken::Algorithm,
    claims: &serde_json::Value,
) -> String {
    let mut h = jsonwebtoken::Header::new(alg);
    h.kid = Some(kid.to_owned());
    jsonwebtoken::encode(&h, claims, &jsonwebtoken::EncodingKey::from_rsa_der(&k.der))
        .expect("sign")
}

static KEY: std::sync::OnceLock<TestKey> = std::sync::OnceLock::new();
fn shared_key() -> &'static TestKey {
    KEY.get_or_init(|| test_key("k1"))
}

fn jwks_for(k: &TestKey) -> Jwks {
    Jwks::from_json(&json!({"keys": [k.jwk.clone()]}).to_string(), policy()).expect("jwks")
}

#[test]
fn jwt_valid_scopes_parsed() {
    let k = shared_key();
    let t = sign(k, "k1", jsonwebtoken::Algorithm::RS256, &good_claims());
    let c: Claims = verify_jwt(&t, &jwks_for(k)).expect("valid");
    assert_eq!(c.sub, "42");
    assert_eq!(c.tenant, "5");
    assert_eq!(c.scope, vec!["proxy:use", "quota:read"]);
    assert_eq!(c.teams, vec!["7"]);
    assert_eq!(c.roles, vec!["user"]);
    let ctx = UserContext::from_claims(&c).expect("ctx");
    assert_eq!(
        (ctx.user_id, ctx.org_id, ctx.auth_mode),
        (42, 5, AuthMode::Jwt)
    );
    assert!(ctx.scopes.contains("proxy:use"));
    assert_eq!(ctx.teams, vec![7]);
}

#[test]
fn jwt_scope_as_space_separated_string_is_accepted() {
    let k = shared_key();
    let mut cl = good_claims();
    cl["scope"] = json!("proxy:use quota:read");
    cl["aud"] = json!([AUD, "other"]);
    let c = verify_jwt(
        &sign(k, "k1", jsonwebtoken::Algorithm::RS256, &cl),
        &jwks_for(k),
    )
    .expect("ok");
    assert_eq!(c.scope, vec!["proxy:use", "quota:read"]);
}

#[test]
fn jwt_missing_tenant_rejects() {
    let k = shared_key();
    for mutate in [
        |c: &mut serde_json::Value| {
            c.as_object_mut().unwrap().remove("tenant");
        },
        |c: &mut serde_json::Value| {
            c["tenant"] = json!("");
        },
    ] {
        let mut cl = good_claims();
        mutate(&mut cl);
        let r = verify_jwt(
            &sign(k, "k1", jsonwebtoken::Algorithm::RS256, &cl),
            &jwks_for(k),
        );
        assert!(matches!(r, Err(AuthError::InvalidToken(_))), "{r:?}");
    }
}

#[test]
fn jwt_bad_sig_rejects() {
    let k = shared_key();
    let other = test_key("k1");
    let t = sign(&other, "k1", jsonwebtoken::Algorithm::RS256, &good_claims());
    assert!(matches!(
        verify_jwt(&t, &jwks_for(k)),
        Err(AuthError::InvalidToken(_))
    ));
}

#[test]
fn jwt_claim_validation_rejects() {
    let k = shared_key();
    let jwks = jwks_for(k);
    let bad: Vec<(&str, serde_json::Value)> = vec![
        ("iss", json!("https://evil.test")),
        ("aud", json!("someone-else")),
        ("exp", json!(now() - 3600)),
        ("sub", json!("")),
    ];
    for (field, val) in bad {
        let mut cl = good_claims();
        cl[field] = val;
        let r = verify_jwt(&sign(k, "k1", jsonwebtoken::Algorithm::RS256, &cl), &jwks);
        assert!(
            matches!(r, Err(AuthError::InvalidToken(_))),
            "{field}: {r:?}"
        );
    }
    for field in ["exp", "iss", "aud", "sub", "scope"] {
        let mut cl = good_claims();
        cl.as_object_mut().unwrap().remove(field);
        let r = verify_jwt(&sign(k, "k1", jsonwebtoken::Algorithm::RS256, &cl), &jwks);
        assert!(
            matches!(r, Err(AuthError::InvalidToken(_))),
            "missing {field}: {r:?}"
        );
    }
}

#[test]
fn jwt_unknown_kid_hs256_and_garbage_reject() {
    let k = shared_key();
    let jwks = jwks_for(k);
    let t = sign(k, "nope", jsonwebtoken::Algorithm::RS256, &good_claims());
    assert!(matches!(verify_jwt(&t, &jwks), Err(AuthError::UnknownKid)));
    // No kid header at all.
    let nokid = jsonwebtoken::encode(
        &jsonwebtoken::Header::new(jsonwebtoken::Algorithm::RS256),
        &good_claims(),
        &jsonwebtoken::EncodingKey::from_rsa_der(&k.der),
    )
    .unwrap();
    assert!(matches!(
        verify_jwt(&nokid, &jwks),
        Err(AuthError::InvalidToken(_))
    ));
    // alg-confusion: HS256 signed with the public modulus must never verify.
    let mut h = jsonwebtoken::Header::new(jsonwebtoken::Algorithm::HS256);
    h.kid = Some("k1".into());
    let hs = jsonwebtoken::encode(
        &h,
        &good_claims(),
        &jsonwebtoken::EncodingKey::from_secret(b"x"),
    )
    .unwrap();
    assert!(matches!(
        verify_jwt(&hs, &jwks),
        Err(AuthError::InvalidToken(_))
    ));
    assert!(matches!(
        verify_jwt("not.a.jwt", &jwks),
        Err(AuthError::InvalidToken(_))
    ));
    assert!(matches!(
        verify_jwt(&"a".repeat(20_000), &jwks),
        Err(AuthError::InvalidToken(_))
    ));
}

#[test]
fn user_context_from_claims_fails_closed() {
    let mk = |sub: &str, tenant: &str, roles: Vec<&str>| Claims {
        sub: sub.into(),
        iss: ISS.into(),
        aud: vec![AUD.into()],
        iat: None,
        exp: now() + 60,
        scope: vec!["proxy:use".into()],
        tenant: tenant.into(),
        teams: vec!["x".into(), "3".into()],
        roles: roles.into_iter().map(Into::into).collect(),
    };
    assert!(matches!(
        UserContext::from_claims(&mk("abc", "5", vec!["user"])),
        Err(AuthError::InvalidToken(_))
    ));
    assert!(matches!(
        UserContext::from_claims(&mk("1", "default", vec!["user"])),
        Err(AuthError::InvalidToken(_))
    ));
    let ctx = UserContext::from_claims(&mk("1", "5", vec![])).expect("ok");
    assert_eq!(ctx.teams, vec![3], "non-numeric team entries dropped");
}

#[test]
fn rbac_scope_bundles_match_python() {
    use auth::rbac::{role_scopes, Role};
    assert!(role_scopes(Role::Admin).contains(&"apikey:admin"));
    assert!(role_scopes(Role::User).contains(&"proxy:use"));
    assert!(!role_scopes(Role::User).contains(&"apikey:admin"));
    assert_eq!(Role::parse("resource_manager"), Some(Role::ResourceManager));
    assert_eq!(Role::parse("Admin"), None);
}

#[tokio::test]
async fn jwks_cache_refetches_on_rotation_and_fails_closed() {
    use auth::{JwksCache, JwksFetcher};
    use std::sync::{
        atomic::{AtomicUsize, Ordering},
        Arc, Mutex,
    };
    use std::time::Duration;

    struct Fake {
        body: Mutex<Result<String, String>>,
        calls: AtomicUsize,
    }
    struct Shared(Arc<Fake>);
    impl JwksFetcher for Shared {
        async fn fetch(&self) -> Result<String, AuthError> {
            self.0.calls.fetch_add(1, Ordering::SeqCst);
            self.0
                .body
                .lock()
                .unwrap()
                .clone()
                .map_err(AuthError::JwksUnavailable)
        }
    }
    let k1 = shared_key();
    let k2 = test_key("k2");
    let fake = Arc::new(Fake {
        body: Mutex::new(Ok(json!({"keys":[k1.jwk.clone()]}).to_string())),
        calls: AtomicUsize::new(0),
    });
    let cache = JwksCache::new(
        Shared(fake.clone()),
        policy(),
        Duration::from_secs(300),
        Duration::ZERO,
    );

    let t1 = sign(k1, "k1", jsonwebtoken::Algorithm::RS256, &good_claims());
    assert!(cache.verify(&t1).await.is_ok());
    assert!(cache.verify(&t1).await.is_ok());
    assert_eq!(
        fake.calls.load(Ordering::SeqCst),
        1,
        "second verify served from cache"
    );

    // Rotation: unknown kid triggers exactly one refetch, then verifies.
    *fake.body.lock().unwrap() = Ok(json!({"keys":[k1.jwk.clone(), k2.jwk.clone()]}).to_string());
    let t2 = sign(&k2, "k2", jsonwebtoken::Algorithm::RS256, &good_claims());
    assert!(cache.verify(&t2).await.is_ok());
    assert_eq!(fake.calls.load(Ordering::SeqCst), 2);

    // Still-unknown kid: one refetch, then reject.
    let t3 = sign(k1, "ghost", jsonwebtoken::Algorithm::RS256, &good_claims());
    assert!(matches!(
        cache.verify(&t3).await,
        Err(AuthError::UnknownKid)
    ));

    // Endpoint down + kid not cached => fail closed, never accept.
    *fake.body.lock().unwrap() = Err("down".into());
    assert!(cache.verify(&t3).await.is_err());
    // ...but a cached kid keeps verifying through the outage.
    assert!(cache.verify(&t1).await.is_ok());

    // A fetch that returns garbage must not poison the cache.
    *fake.body.lock().unwrap() = Ok("not json".into());
    assert!(cache.verify(&t3).await.is_err());
    assert!(cache.verify(&t1).await.is_ok());
}

#[tokio::test]
async fn jwks_cache_min_refetch_interval_blocks_kid_spray() {
    use auth::{JwksCache, JwksFetcher};
    use std::sync::{
        atomic::{AtomicUsize, Ordering},
        Arc,
    };
    use std::time::Duration;
    struct Counting(Arc<AtomicUsize>, String);
    impl JwksFetcher for Counting {
        async fn fetch(&self) -> Result<String, AuthError> {
            self.0.fetch_add(1, Ordering::SeqCst);
            Ok(self.1.clone())
        }
    }
    let k = shared_key();
    let n = Arc::new(AtomicUsize::new(0));
    let cache = JwksCache::new(
        Counting(n.clone(), json!({"keys":[k.jwk.clone()]}).to_string()),
        policy(),
        Duration::from_secs(300),
        Duration::from_secs(60),
    );
    for i in 0..5 {
        let t = sign(
            k,
            &format!("spray{i}"),
            jsonwebtoken::Algorithm::RS256,
            &good_claims(),
        );
        assert!(matches!(cache.verify(&t).await, Err(AuthError::UnknownKid)));
    }
    assert_eq!(
        n.load(Ordering::SeqCst),
        1,
        "unknown-kid spray must not hammer the JWKS endpoint"
    );
}

#[test]
fn rbac_all_roles_and_team_roles_parse_and_expand() {
    use auth::rbac::{role_scopes, team_scopes, Role, TeamRole};
    for (s, r) in [
        ("admin", Role::Admin),
        ("resource_manager", Role::ResourceManager),
        ("reporter", Role::Reporter),
        ("user", Role::User),
    ] {
        assert_eq!(Role::parse(s), Some(r));
        assert!(role_scopes(r).contains(&"system:health"), "{s}");
    }
    assert!(role_scopes(Role::Reporter).contains(&"analytics:security"));
    for (s, r) in [
        ("owner", TeamRole::Owner),
        ("admin", TeamRole::Admin),
        ("member", TeamRole::Member),
        ("viewer", TeamRole::Viewer),
    ] {
        assert_eq!(TeamRole::parse(s), Some(r));
        assert!(team_scopes(r).contains(&"team:read"), "{s}");
    }
    assert!(team_scopes(TeamRole::Owner).contains(&"team:transfer_owner"));
    assert!(!team_scopes(TeamRole::Viewer).contains(&"team:use"));
    assert_eq!(TeamRole::parse("Owner"), None);
}

#[test]
fn jwks_parsing_rejects_unusable_sets_and_debug_hides_keys() {
    assert!(matches!(
        Jwks::from_json("nope", policy()),
        Err(AuthError::JwksUnavailable(_))
    ));
    assert!(matches!(
        Jwks::from_json(r#"{"keys":[{"kty":"oct","k":"AAAA","kid":"s"}]}"#, policy()),
        Err(AuthError::JwksUnavailable(_))
    ));
    let k = shared_key();
    // A kid-less RSA key is skipped, leaving nothing usable.
    let mut nokid = k.jwk.clone();
    nokid.as_object_mut().unwrap().remove("kid");
    assert!(Jwks::from_json(&json!({"keys":[nokid]}).to_string(), policy()).is_err());
    let dbg = format!("{:?}", jwks_for(k));
    assert!(dbg.contains("k1") && !dbg.contains(k.jwk["n"].as_str().unwrap()));
}

#[test]
fn jwt_policy_reads_oidc_env_with_python_defaults() {
    // The only test touching OIDC_* vars.
    std::env::remove_var("OIDC_ISSUER_URL");
    std::env::remove_var("OIDC_CLIENT_ID");
    assert_eq!(
        JwtPolicy::from_env(),
        JwtPolicy::new("https://waddleai.localhost.local", "waddleai-api")
    );
    std::env::set_var("OIDC_ISSUER_URL", ISS);
    std::env::set_var("OIDC_CLIENT_ID", "");
    assert_eq!(JwtPolicy::from_env(), JwtPolicy::new(ISS, "waddleai-api"));
    std::env::remove_var("OIDC_ISSUER_URL");
    std::env::remove_var("OIDC_CLIENT_ID");
}

#[tokio::test]
async fn jwks_cache_ttl_expiry_refreshes_and_survives_outage() {
    use auth::{JwksCache, JwksFetcher};
    use std::sync::{
        atomic::{AtomicBool, AtomicUsize, Ordering},
        Arc,
    };
    use std::time::Duration;
    struct Flaky {
        body: String,
        down: Arc<AtomicBool>,
        calls: Arc<AtomicUsize>,
    }
    impl JwksFetcher for Flaky {
        async fn fetch(&self) -> Result<String, AuthError> {
            self.calls.fetch_add(1, Ordering::SeqCst);
            if self.down.load(Ordering::SeqCst) {
                Err(AuthError::JwksUnavailable("down".into()))
            } else {
                Ok(self.body.clone())
            }
        }
    }
    let k = shared_key();
    let (down, calls) = (
        Arc::new(AtomicBool::new(false)),
        Arc::new(AtomicUsize::new(0)),
    );
    let cache = JwksCache::new(
        Flaky {
            body: json!({"keys":[k.jwk.clone()]}).to_string(),
            down: down.clone(),
            calls: calls.clone(),
        },
        policy(),
        Duration::ZERO, // always stale: every verify attempts a refresh
        Duration::ZERO, // ...and the throttle is open
    )
    // Default max-stale is 4x TTL == 0 here; give the stale set a real serving window.
    .with_max_stale(Duration::from_secs(60));
    let t = sign(k, "k1", jsonwebtoken::Algorithm::RS256, &good_claims());
    assert!(cache.verify(&t).await.is_ok());
    assert!(cache.verify(&t).await.is_ok());
    // Stale-while-revalidate: the second verify served the cached set and refreshed in the
    // background; await that task deterministically.
    cache.settle_background().await;
    assert_eq!(
        calls.load(Ordering::SeqCst),
        2,
        "stale cache revalidates in the background"
    );
    down.store(true, Ordering::SeqCst);
    assert!(
        cache.verify(&t).await.is_ok(),
        "stale-but-present keys serve through an outage"
    );

    // Cold cache + outage => fail closed.
    let cold = JwksCache::new(
        Flaky {
            body: String::new(),
            down,
            calls,
        },
        policy(),
        Duration::from_secs(300),
        Duration::from_secs(60),
    );
    assert!(matches!(
        cold.verify(&t).await,
        Err(AuthError::JwksUnavailable(_))
    ));
}

#[tokio::test]
async fn wa_key_teams_disabled_gives_no_team_scopes() {
    let c = &conn().await;
    let s = seed_user(c, "team-off", "user", true).await;
    exec(
        c,
        "DELETE FROM teams WHERE organization_id = $1 AND name = 'off-team'",
        vec![s.org_id.into()],
    )
    .await;
    exec(
        c,
        "INSERT INTO teams (organization_id, name, enabled) VALUES ($1, 'off-team', true)",
        vec![s.org_id.into()],
    )
    .await;
    let team_id = scalar_i32(
        c,
        "SELECT id FROM teams WHERE organization_id = $1 AND name = 'off-team'",
        vec![s.org_id.into()],
    )
    .await;
    exec(c, "INSERT INTO team_members (team_id, user_id, organization_id, team_role) VALUES ($1, $2, $3, 'owner')",
        vec![team_id.into(), s.user_id.into(), s.org_id.into()]).await;
    let (k, h) = mint("aa00000000000007", "s7");
    seed_key(c, &s, "aa00000000000007", &h, true, None, Some(team_id)).await;
    let on = authenticate_api_key(c, &k, true).await.expect("ok");
    assert!(on.scopes.contains("team:delete"));
    let off = authenticate_api_key(c, &k, false).await.expect("ok");
    assert_eq!((off.team_id, off.team_role.clone()), (None, None));
    assert!(!off
        .scopes
        .iter()
        .any(|s| s.starts_with("team:") && s != "team:org_admin"));
    exec(
        c,
        "DELETE FROM team_members WHERE team_id = $1",
        vec![team_id.into()],
    )
    .await;
    exec(
        c,
        "DELETE FROM api_keys WHERE team_id = $1",
        vec![team_id.into()],
    )
    .await;
    exec(c, "DELETE FROM teams WHERE id = $1", vec![team_id.into()]).await;
}

#[tokio::test]
async fn wa_key_expiry_uses_naive_utc_basis() {
    use chrono::{Duration as D, Utc};
    let c = &conn().await;
    let s = seed_user(c, "expiry", "user", true).await;
    let fmt = |d: D| (Utc::now() + d).format("%Y-%m-%d %H:%M:%S").to_string();
    let (k1, h1) = mint("aa00000000000008", "s8");
    seed_key(
        c,
        &s,
        "aa00000000000008",
        &h1,
        true,
        Some(&fmt(D::minutes(-5))),
        None,
    )
    .await;
    assert!(matches!(
        authenticate_api_key(c, &k1, true).await,
        Err(AuthError::InvalidApiKey)
    ));
    let (k2, h2) = mint("aa00000000000009", "s9");
    seed_key(
        c,
        &s,
        "aa00000000000009",
        &h2,
        true,
        Some(&fmt(D::minutes(5))),
        None,
    )
    .await;
    assert!(authenticate_api_key(c, &k2, true).await.is_ok());
}

#[tokio::test]
async fn wa_key_nul_secret_and_overlong_reject_without_db() {
    let c = &conn().await;
    // Pin the PARSE-time guards directly: the end-to-end InvalidApiKey alone would also
    // result from a bcrypt mismatch and so could not catch a removed guard.
    let nul = format!("wa-{PY_KEY_ID}-a\0b");
    assert_eq!(auth::parse_wa_key_id(&nul), None);
    let long = format!("{PY_KEY}{}", "x".repeat(20));
    assert!(long.len() > 72);
    assert_eq!(auth::parse_wa_key_id(&long), None);
    // Positive control: the exact minted key parses to its key_id.
    assert_eq!(auth::parse_wa_key_id(PY_KEY), Some(PY_KEY_ID));
    for bad in [&nul, &long] {
        assert!(matches!(
            authenticate_api_key(c, bad, true).await,
            Err(AuthError::InvalidApiKey)
        ));
    }
}

#[test]
fn warm_up_is_idempotent() {
    auth::warm_up();
    auth::warm_up();
}

#[test]
fn jwt_future_nbf_rejects() {
    let k = shared_key();
    let mut cl = good_claims();
    cl["nbf"] = json!(now() + 3600);
    let r = verify_jwt(
        &sign(k, "k1", jsonwebtoken::Algorithm::RS256, &cl),
        &jwks_for(k),
    );
    assert!(matches!(r, Err(AuthError::InvalidToken(_))), "{r:?}");
    let mut ok = good_claims();
    ok["nbf"] = json!(now() - 10);
    assert!(verify_jwt(
        &sign(k, "k1", jsonwebtoken::Algorithm::RS256, &ok),
        &jwks_for(k)
    )
    .is_ok());
}

#[test]
fn user_context_rejects_zero_and_negative_ids() {
    let mk = |sub: &str, tenant: &str| Claims {
        sub: sub.into(),
        iss: ISS.into(),
        aud: vec![AUD.into()],
        iat: None,
        exp: now() + 60,
        scope: vec!["proxy:use".into()],
        tenant: tenant.into(),
        teams: vec![],
        roles: vec![],
    };
    for (sub, tenant) in [("0", "5"), ("-1", "5"), ("1", "0"), ("1", "-3")] {
        assert!(
            matches!(
                UserContext::from_claims(&mk(sub, tenant)),
                Err(AuthError::InvalidToken(_))
            ),
            "{sub}/{tenant}"
        );
    }
    assert!(UserContext::from_claims(&mk("1", "1")).is_ok());
}

#[test]
fn jwks_skips_encryption_keys() {
    let k = shared_key();
    let mut enc = k.jwk.clone();
    enc["use"] = json!("enc");
    assert!(Jwks::from_json(&json!({"keys":[enc]}).to_string(), policy()).is_err());
    let mut nouse = k.jwk.clone();
    nouse.as_object_mut().unwrap().remove("use");
    assert!(Jwks::from_json(&json!({"keys":[nouse]}).to_string(), policy()).is_ok());
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn jwks_cache_concurrent_unknown_kid_spray_fetches_once() {
    use auth::{JwksCache, JwksFetcher};
    use std::sync::{
        atomic::{AtomicUsize, Ordering},
        Arc,
    };
    use std::time::Duration;
    struct Slow(Arc<AtomicUsize>, String);
    impl JwksFetcher for Slow {
        async fn fetch(&self) -> Result<String, AuthError> {
            self.0.fetch_add(1, Ordering::SeqCst);
            tokio::time::sleep(Duration::from_millis(60)).await;
            Ok(self.1.clone())
        }
    }
    let k = shared_key();
    let n = Arc::new(AtomicUsize::new(0));
    let cache = Arc::new(JwksCache::new(
        Slow(n.clone(), json!({"keys":[k.jwk.clone()]}).to_string()),
        policy(),
        Duration::from_secs(300),
        Duration::from_millis(150),
    ));
    let warm = sign(k, "k1", jsonwebtoken::Algorithm::RS256, &good_claims());
    assert!(cache.verify(&warm).await.is_ok());
    tokio::time::sleep(Duration::from_millis(200)).await; // throttle window open
    let mut tasks = vec![];
    for i in 0..20 {
        let (cache, t) = (
            cache.clone(),
            sign(
                k,
                &format!("ghost{i}"),
                jsonwebtoken::Algorithm::RS256,
                &good_claims(),
            ),
        );
        tasks.push(tokio::spawn(async move { cache.verify(&t).await }));
    }
    for t in tasks {
        assert!(matches!(t.await.unwrap(), Err(AuthError::UnknownKid)));
    }
    assert_eq!(
        n.load(Ordering::SeqCst),
        2,
        "warm fetch + exactly one refetch for the whole spray"
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn jwks_cache_stale_refresh_coalesces_and_throttles() {
    use auth::{JwksCache, JwksFetcher};
    use std::sync::{
        atomic::{AtomicUsize, Ordering},
        Arc,
    };
    use std::time::Duration;
    struct Slow(Arc<AtomicUsize>, String);
    impl JwksFetcher for Slow {
        async fn fetch(&self) -> Result<String, AuthError> {
            self.0.fetch_add(1, Ordering::SeqCst);
            tokio::time::sleep(Duration::from_millis(60)).await;
            Ok(self.1.clone())
        }
    }
    let k = shared_key();
    let n = Arc::new(AtomicUsize::new(0));
    // ttl tiny => stale almost at once; throttle 300ms => at most one refresh per window.
    let cache = Arc::new(JwksCache::new(
        Slow(n.clone(), json!({"keys":[k.jwk.clone()]}).to_string()),
        policy(),
        Duration::from_millis(10),
        Duration::from_millis(300),
    ));
    let t = sign(k, "k1", jsonwebtoken::Algorithm::RS256, &good_claims());
    assert!(cache.verify(&t).await.is_ok());
    tokio::time::sleep(Duration::from_millis(350)).await; // stale AND throttle open
    let mut tasks = vec![];
    for _ in 0..20 {
        let (cache, t) = (cache.clone(), t.clone());
        tasks.push(tokio::spawn(async move { cache.verify(&t).await }));
    }
    for h in tasks {
        assert!(h.await.unwrap().is_ok(), "stale keys keep serving");
    }
    cache.settle_background().await; // the one background refresh
    assert_eq!(
        n.load(Ordering::SeqCst),
        2,
        "20 concurrent stale callers coalesce into one refresh"
    );
}

#[tokio::test]
async fn jwks_cache_garbage_token_never_triggers_fetch() {
    use auth::{JwksCache, JwksFetcher};
    use std::sync::{
        atomic::{AtomicUsize, Ordering},
        Arc,
    };
    use std::time::Duration;
    struct C(Arc<AtomicUsize>);
    impl JwksFetcher for C {
        async fn fetch(&self) -> Result<String, AuthError> {
            self.0.fetch_add(1, Ordering::SeqCst);
            Err(AuthError::JwksUnavailable("x".into()))
        }
    }
    let n = Arc::new(AtomicUsize::new(0));
    let cache = JwksCache::new(C(n.clone()), policy(), Duration::ZERO, Duration::ZERO);
    for bad in ["", "garbage", "a.b.c"] {
        assert!(matches!(
            cache.verify(bad).await,
            Err(AuthError::InvalidToken(_))
        ));
    }
    assert_eq!(n.load(Ordering::SeqCst), 0);
}

#[tokio::test]
async fn jwks_cache_hung_fetch_times_out_and_cached_keys_still_verify() {
    use auth::{JwksCache, JwksFetcher};
    use std::sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    };
    use std::time::Duration;
    struct Hang(Arc<AtomicBool>, String);
    impl JwksFetcher for Hang {
        async fn fetch(&self) -> Result<String, AuthError> {
            if self.0.load(Ordering::SeqCst) {
                std::future::pending::<()>().await;
            }
            Ok(self.1.clone())
        }
    }
    let k = shared_key();
    let hang = Arc::new(AtomicBool::new(false));
    let cache = Arc::new(
        JwksCache::new(
            Hang(hang.clone(), json!({"keys":[k.jwk.clone()]}).to_string()),
            policy(),
            Duration::from_secs(300),
            Duration::ZERO,
        )
        .with_fetch_timeout(Duration::from_millis(80)),
    );
    let good = sign(k, "k1", jsonwebtoken::Algorithm::RS256, &good_claims());
    assert!(cache.verify(&good).await.is_ok());
    hang.store(true, Ordering::SeqCst);
    let ghost = sign(k, "ghost", jsonwebtoken::Algorithm::RS256, &good_claims());
    let c2 = cache.clone();
    let spray = tokio::spawn(async move { c2.verify(&ghost).await });
    // While the hung refetch holds refresh_lock, a cached-key token still verifies promptly.
    tokio::time::sleep(Duration::from_millis(10)).await;
    let started = std::time::Instant::now();
    assert!(cache.verify(&good).await.is_ok());
    assert!(
        started.elapsed() < Duration::from_millis(50),
        "cached verify must not wait on the hung fetch"
    );
    let r = tokio::time::timeout(Duration::from_secs(2), spray)
        .await
        .expect("bounded")
        .unwrap();
    assert!(matches!(r, Err(AuthError::JwksUnavailable(_))), "{r:?}");
}

#[test]
fn tenant_claim_is_canonicalized_to_match_api_key_path() {
    let mk = |tenant: &str| Claims {
        sub: "1".into(),
        iss: ISS.into(),
        aud: vec![AUD.into()],
        iat: None,
        exp: now() + 60,
        scope: vec!["proxy:use".into()],
        tenant: tenant.into(),
        teams: vec![],
        roles: vec![],
    };
    // API-key path emits `org_id.to_string()`; every spelling of 5 must collapse to "5".
    for spelling in ["5", "05", "+5", "005"] {
        let ctx = UserContext::from_claims(&mk(spelling)).expect(spelling);
        assert_eq!((ctx.org_id, ctx.tenant.as_str()), (5, "5"), "{spelling}");
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn jwks_throttle_holds_when_fetch_future_is_dropped_mid_flight() {
    use auth::{JwksCache, JwksFetcher};
    use std::sync::{
        atomic::{AtomicBool, AtomicUsize, Ordering},
        Arc,
    };
    use std::time::Duration;
    struct F(Arc<AtomicUsize>, Arc<AtomicBool>, String);
    impl JwksFetcher for F {
        async fn fetch(&self) -> Result<String, AuthError> {
            self.0.fetch_add(1, Ordering::SeqCst);
            if self.1.load(Ordering::SeqCst) {
                std::future::pending::<()>().await;
            }
            Ok(self.2.clone())
        }
    }
    let k = shared_key();
    let (n, hang) = (
        Arc::new(AtomicUsize::new(0)),
        Arc::new(AtomicBool::new(false)),
    );
    let cache = JwksCache::new(
        F(
            n.clone(),
            hang.clone(),
            json!({"keys":[k.jwk.clone()]}).to_string(),
        ),
        policy(),
        Duration::from_secs(300),
        Duration::from_millis(300),
    );
    let warm = sign(k, "k1", jsonwebtoken::Algorithm::RS256, &good_claims());
    assert!(cache.verify(&warm).await.is_ok());
    tokio::time::sleep(Duration::from_millis(350)).await; // throttle window open
    hang.store(true, Ordering::SeqCst);
    // A caller that disconnects mid-fetch (its future is dropped)...
    let ghost = sign(k, "ghost0", jsonwebtoken::Algorithm::RS256, &good_claims());
    let dropped = tokio::time::timeout(Duration::from_millis(30), cache.verify(&ghost)).await;
    assert!(
        dropped.is_err(),
        "fetch was in flight when the caller went away"
    );
    assert_eq!(n.load(Ordering::SeqCst), 2);
    // ...must still have consumed the throttle window: further sprayers fetch nothing.
    for i in 1..6 {
        let t = sign(
            k,
            &format!("ghost{i}"),
            jsonwebtoken::Algorithm::RS256,
            &good_claims(),
        );
        assert!(matches!(cache.verify(&t).await, Err(AuthError::UnknownKid)));
    }
    assert_eq!(
        n.load(Ordering::SeqCst),
        2,
        "dropped attempt still counted against min_refetch"
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn jwks_ttl_expiry_with_hung_issuer_does_not_stall_cached_kid() {
    use auth::{JwksCache, JwksFetcher};
    use std::sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    };
    use std::time::Duration;
    struct F(Arc<AtomicBool>, String);
    impl JwksFetcher for F {
        async fn fetch(&self) -> Result<String, AuthError> {
            if self.0.load(Ordering::SeqCst) {
                std::future::pending::<()>().await;
            }
            Ok(self.1.clone())
        }
    }
    let k = shared_key();
    let hang = Arc::new(AtomicBool::new(false));
    // Default 5s fetch timeout: an inline refresh would stall for ~5s.
    let cache = JwksCache::new(
        F(hang.clone(), json!({"keys":[k.jwk.clone()]}).to_string()),
        policy(),
        Duration::from_millis(10),
        Duration::ZERO,
    );
    let t = sign(k, "k1", jsonwebtoken::Algorithm::RS256, &good_claims());
    assert!(cache.verify(&t).await.is_ok());
    tokio::time::sleep(Duration::from_millis(30)).await; // TTL expired
    hang.store(true, Ordering::SeqCst);
    for _ in 0..3 {
        let started = std::time::Instant::now();
        assert!(cache.verify(&t).await.is_ok());
        assert!(
            started.elapsed() < Duration::from_millis(200),
            "cached kid must not wait on refresh"
        );
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn jwks_cold_fetch_survives_caller_drop_and_does_not_lock_out() {
    use auth::{JwksCache, JwksFetcher};
    use std::sync::{
        atomic::{AtomicUsize, Ordering},
        Arc,
    };
    use std::time::Duration;
    struct F(Arc<AtomicUsize>, Arc<AtomicUsize>, String);
    impl JwksFetcher for F {
        async fn fetch(&self) -> Result<String, AuthError> {
            self.0.fetch_add(1, Ordering::SeqCst);
            tokio::time::sleep(Duration::from_millis(150)).await;
            self.1.fetch_add(1, Ordering::SeqCst); // reached only if the fetch ran to completion
            Ok(self.2.clone())
        }
    }
    let k = shared_key();
    let (started, finished) = (Arc::new(AtomicUsize::new(0)), Arc::new(AtomicUsize::new(0)));
    // Long throttle: a cancelled-but-charged attempt would lock everyone out for 60s.
    let cache = JwksCache::new(
        F(
            started.clone(),
            finished.clone(),
            json!({"keys":[k.jwk.clone()]}).to_string(),
        ),
        policy(),
        Duration::from_secs(300),
        Duration::from_secs(60),
    );
    let t = sign(k, "k1", jsonwebtoken::Algorithm::RS256, &good_claims());
    // Caller disconnects mid cold-start fetch.
    let dropped = tokio::time::timeout(Duration::from_millis(30), cache.verify(&t)).await;
    assert!(dropped.is_err(), "caller was dropped mid-fetch");
    assert_eq!(started.load(Ordering::SeqCst), 1);
    // A later caller (arriving mid-flight) waits for the detached fetch and authenticates.
    assert!(
        cache.verify(&t).await.is_ok(),
        "no lockout window after a dropped cold fetch"
    );
    assert_eq!(
        finished.load(Ordering::SeqCst),
        1,
        "the detached fetch completed"
    );
    assert_eq!(started.load(Ordering::SeqCst), 1, "and was not repeated");
}

#[tokio::test]
async fn jwks_max_stale_stops_serving_old_keys_when_refresh_keeps_failing() {
    use auth::{JwksCache, JwksFetcher};
    use std::sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    };
    use std::time::Duration;
    struct F(Arc<AtomicBool>, String);
    impl JwksFetcher for F {
        async fn fetch(&self) -> Result<String, AuthError> {
            if self.0.load(Ordering::SeqCst) {
                Err(AuthError::JwksUnavailable("down".into()))
            } else {
                Ok(self.1.clone())
            }
        }
    }
    let k = shared_key();
    let down = Arc::new(AtomicBool::new(false));
    let cache = JwksCache::new(
        F(down.clone(), json!({"keys":[k.jwk.clone()]}).to_string()),
        policy(),
        Duration::from_millis(10),
        Duration::ZERO,
    )
    .with_max_stale(Duration::from_millis(150));
    let t = sign(k, "k1", jsonwebtoken::Algorithm::RS256, &good_claims());
    assert!(cache.verify(&t).await.is_ok());
    down.store(true, Ordering::SeqCst);
    tokio::time::sleep(Duration::from_millis(30)).await; // past TTL, within max-stale
    assert!(
        cache.verify(&t).await.is_ok(),
        "stale-while-revalidate within max-stale"
    );
    cache.settle_background().await;
    tokio::time::sleep(Duration::from_millis(200)).await; // past max-stale
    let r = cache.verify(&t).await;
    assert!(matches!(r, Err(AuthError::JwksUnavailable(_))), "{r:?}");
    // Recovery: once the issuer is back a refresh succeeds and the kid verifies again.
    down.store(false, Ordering::SeqCst);
    assert!(cache.verify(&t).await.is_ok());
}
