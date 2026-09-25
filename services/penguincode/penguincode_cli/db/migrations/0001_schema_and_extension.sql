-- 0001_schema_and_extension.sql
--
-- Creates the penguincode schema and enables the pgvector extension on the
-- shared WaddleAI Postgres instance. penguincode owns this schema only (not
-- the waddleai app role/schema) -- see docs/superpowers/specs/
-- 2026-09-25-penguincode-knowledge-platform-design.md section 9.
--
-- Idempotent: safe to run multiple times against the same database.
CREATE SCHEMA IF NOT EXISTS penguincode;

CREATE EXTENSION IF NOT EXISTS vector;
