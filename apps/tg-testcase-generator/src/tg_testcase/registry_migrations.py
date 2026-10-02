"""Separate registry schema; never touches acquisition migrations."""
VERSION = 1
SQL = '''
CREATE TABLE IF NOT EXISTS current_policy (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), epoch INTEGER NOT NULL,
 digest TEXT NOT NULL, canonical_json BLOB NOT NULL, published_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS policy_history (epoch INTEGER PRIMARY KEY, digest TEXT NOT NULL,
 canonical_json BLOB NOT NULL, published_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS credential_head (credential_ref TEXT PRIMARY KEY, payload BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS credential_history (credential_ref TEXT NOT NULL, generation INTEGER NOT NULL,
 payload BLOB NOT NULL, PRIMARY KEY(credential_ref,generation));
CREATE TABLE IF NOT EXISTS verifications (verification_ref TEXT PRIMARY KEY, payload BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS mutations (mutation_id TEXT PRIMARY KEY, digest TEXT NOT NULL, result BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY, payload BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS replay (
 issuer TEXT NOT NULL, bot_instance TEXT NOT NULL, nonce TEXT NOT NULL, update_id INTEGER NOT NULL,
 request_id TEXT UNIQUE NOT NULL, payload BLOB NOT NULL,
 UNIQUE(issuer,bot_instance,nonce), UNIQUE(issuer,bot_instance,update_id));
CREATE TABLE IF NOT EXISTS gateway_updates (
 issuer TEXT NOT NULL, bot_instance TEXT NOT NULL, update_id INTEGER NOT NULL, payload BLOB NOT NULL,
 PRIMARY KEY(issuer,bot_instance,update_id));
CREATE TABLE IF NOT EXISTS clock_state (singleton INTEGER PRIMARY KEY CHECK(singleton=1), high_water INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS jobs (
 job_id TEXT PRIMARY KEY, semantic_key TEXT UNIQUE NOT NULL, batch_id TEXT NOT NULL,
 source_id TEXT NOT NULL, state TEXT NOT NULL, package_id TEXT UNIQUE, payload BLOB NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_job ON jobs(batch_id,source_id)
 WHERE state NOT IN ('committed','cancelled','revoked','expired','failed');
CREATE TABLE IF NOT EXISTS request_jobs (request_id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(job_id), intent_digest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS job_attempts (job_id TEXT NOT NULL REFERENCES jobs(job_id), attempt INTEGER NOT NULL,
 payload BLOB NOT NULL, PRIMARY KEY(job_id,attempt));
CREATE TABLE IF NOT EXISTS consumed_events (event_id TEXT PRIMARY KEY, event_digest TEXT NOT NULL);
'''
