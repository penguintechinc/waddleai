//! Drift gate: regenerate entities from the live (Alembic-migrated) schema and
//! diff against the committed `src/entities/`. Fails on any difference, so an
//! Alembic migration the entities don't reflect cannot merge.
//! Needs `TEST_DATABASE_URL` and `sea-orm-cli` on PATH; fails loudly otherwise.
use std::path::{Path, PathBuf};
use std::process::Command;

fn read_tree(root: &Path) -> Vec<(PathBuf, String)> {
    let mut out = Vec::new();
    let mut stack = vec![root.to_path_buf()];
    while let Some(d) = stack.pop() {
        for e in std::fs::read_dir(&d).expect("read_dir") {
            let p = e.expect("entry").path();
            if p.is_dir() {
                stack.push(p);
            } else {
                let rel = p.strip_prefix(root).expect("prefix").to_path_buf();
                out.push((rel, std::fs::read_to_string(&p).expect("read")));
            }
        }
    }
    out.sort();
    out
}

#[test]
fn entities_match_live_schema() {
    assert!(
        std::env::var("TEST_DATABASE_URL").is_ok(),
        "TEST_DATABASE_URL must point at an Alembic-migrated Postgres"
    );
    let crate_dir = Path::new(env!("CARGO_MANIFEST_DIR"));
    let script = crate_dir.join("../../scripts/gen-entities.sh");
    let tmp = std::env::temp_dir().join(format!("db-entity-drift-{}", std::process::id()));
    let status = Command::new("bash")
        .arg(&script)
        .arg(&tmp)
        .status()
        .expect("run gen-entities.sh");
    assert!(status.success(), "gen-entities.sh failed");

    let fresh = read_tree(&tmp);
    let committed = read_tree(&crate_dir.join("src/entities"));
    let _ = std::fs::remove_dir_all(&tmp);

    assert!(!committed.is_empty(), "no committed entities examined");
    let f: Vec<_> = fresh.iter().map(|(p, _)| p).collect();
    let c: Vec<_> = committed.iter().map(|(p, _)| p).collect();
    assert_eq!(f, c, "entity file set drifted; run scripts/gen-entities.sh");
    for ((p, a), (_, b)) in fresh.iter().zip(committed.iter()) {
        assert_eq!(
            a,
            b,
            "entity {} drifted from live schema; run scripts/gen-entities.sh",
            p.display()
        );
    }
}
