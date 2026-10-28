//! Connection tests. `connect_pools_and_pings` needs `TEST_DATABASE_URL` (a
//! Postgres with the Alembic schema applied) and FAILS loudly when it is unset.
use db::{connect, connect_with, DbError, DbOptions};
use sea_orm::{ConnectionTrait, Statement};
use std::time::Duration;

#[tokio::test]
async fn connect_pools_and_pings() {
    let url = std::env::var("TEST_DATABASE_URL").expect(
        "TEST_DATABASE_URL must point at a migrated Postgres (see rust/scripts/gen-entities.sh)",
    );
    std::env::set_var("DATABASE_URL", &url);
    let cfg = config::Config::from_env().expect("config");
    let conn = connect(&cfg).await.expect("connect");
    let row = conn
        .query_one_raw(Statement::from_string(
            conn.get_database_backend(),
            "SELECT count(*) AS n FROM organizations",
        ))
        .await
        .expect("query")
        .expect("row");
    let n: i64 = row.try_get("", "n").expect("n");
    assert!(n >= 0);
}

#[tokio::test]
async fn connect_retries_then_errors() {
    let opts = DbOptions {
        pool_size: 1,
        max_retries: 3,
        retry_delay: Duration::from_millis(10),
    };
    let start = std::time::Instant::now();
    let err = connect_with("postgres://u:p@127.0.0.1:1/none", &opts)
        .await
        .expect_err("must fail");
    match err {
        DbError::Connect { attempts, .. } => assert_eq!(attempts, 3),
        other => panic!("unexpected {other:?}"),
    }
    // two backoff sleeps: 10ms + 20ms
    assert!(start.elapsed() >= Duration::from_millis(30));
}

#[test]
fn options_from_env_parse_and_reject() {
    // single test owns these vars (no other test reads them)
    std::env::set_var("DB_POOL_SIZE", "7");
    std::env::set_var("DB_MAX_RETRIES", "2");
    std::env::set_var("DB_RETRY_DELAY", "1");
    let o = DbOptions::from_env().expect("ok");
    assert_eq!(
        (o.pool_size, o.max_retries, o.retry_delay),
        (7, 2, Duration::from_secs(1))
    );
    std::env::set_var("DB_POOL_SIZE", "abc");
    assert!(matches!(
        DbOptions::from_env(),
        Err(DbError::InvalidOption {
            var: "DB_POOL_SIZE"
        })
    ));
    for v in ["DB_POOL_SIZE", "DB_MAX_RETRIES", "DB_RETRY_DELAY"] {
        std::env::remove_var(v);
    }
    assert_eq!(
        DbOptions::from_env().expect("defaults"),
        DbOptions::default()
    );
}
