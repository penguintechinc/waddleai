---
name: alembic-fresh-db-chain-from-frozen-baseline
description: gh-215 — empty-DB `alembic upgrade head` works by 001_baseline creating a FROZEN pre-Alembic schema then running the real chain; create_all+stamp head is NOT head-equivalent (no seeds, no migration-only DDL).
metadata:
  type: project
---

`create_all + stamp head` gives a DB missing every seed row (model_registry etc.) and migration-only DDL (embedding_cache.embedding, code_chunks.content_tsv, hook_* tables) — permanently, since stamped DBs never re-run migrations. Fix (PR #293): 001_baseline.py holds a frozen copy of the 04-02 models (23 tables, minus credential_label which 003 adds) + pgvector; real chain 002..head runs on top in ONE transaction (rollback = retry-safe).

**Why:** the chain had never been run end-to-end; walking it found latent bugs: alembic_version VARCHAR(32) vs 39-char rev 003 (widen in 001), 012 re-adds 6 memory_embeddings cols 009b added (add-if-missing), 013 ALTERs model-only `llamacpp_deployments` (create-if-missing).
**How to apply:** known residual ORM-vs-chain drift (not fixed, follow-up): ORM-only cols code_chunks/docs_cache_pages.embedding_json, embedding_cache.provenance; ORM stale ai_providers.api_key + ailb_usage_records table (chain drops them); nullability diffs. Parity test: tests/integration/test_gh215_fresh_db_bootstrap.py. 013/014 unit chain tests still have stale unconditional skips ("chain not whole"). 012 FTS/GIN is on code_chunks (not docs_sources). Removing vector.control via `docker exec` is a cheap real "no pgvector" fixture.
