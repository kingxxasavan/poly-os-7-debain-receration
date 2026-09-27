// The Poly Account database: Postgres. On Vercel it's Neon (Storage › Create Database › Neon,
// which sets DATABASE_URL); locally and in tests it's PGlite, Postgres running in this process.

let dbPromise = null;

export function databaseUrl() {
  return process.env.DATABASE_URL || process.env.POSTGRES_URL || '';
}

async function connect() {
  const url = databaseUrl();
  if (url) {
    const { neon } = await import('@neondatabase/serverless');
    const sql = neon(url);
    return { query: (text, params = []) => sql.query(text, params), kind: 'neon' };
  }
  if (process.env.VERCEL) {
    throw Object.assign(new Error('Poly Account isn’t set up yet: the database is missing.'), { status: 503 });
  }
  const { PGlite } = await import('@electric-sql/pglite');
  const pg = new PGlite(process.env.POLY_DEV_DB || undefined); // a folder keeps dev data; none = in memory
  return { query: async (text, params = []) => (await pg.query(text, params)).rows, kind: 'pglite', pg };
}

export function db() {
  if (!dbPromise) {
    dbPromise = connect().then(async (conn) => {
      await migrate(conn);
      return conn;
    }).catch((err) => {
      dbPromise = null; // try again on the next request
      throw err;
    });
  }
  return dbPromise;
}

export async function q(text, params) {
  return (await db()).query(text, params);
}

export async function one(text, params) {
  return (await q(text, params))[0] || null;
}

// For tests: a fresh in-memory database.
export function resetForTests() {
  dbPromise = null;
}

// ---- schema ---------------------------------------------------------------------------------
// Additive only: new columns and tables are added with IF NOT EXISTS, so every deployment can run it.
const SCHEMA = [
  `CREATE TABLE IF NOT EXISTS users (
     id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
     email text NOT NULL UNIQUE,
     email_verified boolean NOT NULL DEFAULT false,
     name text NOT NULL,
     country text NOT NULL,
     password_hash text NOT NULL,
     password_changed_at timestamptz NOT NULL DEFAULT now(),
     recovery_hash text,
     recovery_created_at timestamptz,
     terms_version text,
     terms_accepted_at timestamptz,
     prefs jsonb NOT NULL DEFAULT '{}'::jsonb,
     created_at timestamptz NOT NULL DEFAULT now()
   )`,
  `CREATE TABLE IF NOT EXISTS sessions (
     id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
     user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
     token_hash text NOT NULL UNIQUE,
     agent text NOT NULL DEFAULT '',
     place text NOT NULL DEFAULT '',
     created_at timestamptz NOT NULL DEFAULT now(),
     last_seen timestamptz NOT NULL DEFAULT now(),
     expires_at timestamptz NOT NULL
   )`,
  `CREATE TABLE IF NOT EXISTS devices (
     id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
     user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
     name text NOT NULL,
     credential_hash text NOT NULL UNIQUE,
     version text NOT NULL DEFAULT '',
     arch text NOT NULL DEFAULT '',
     channel text NOT NULL DEFAULT 'stable',
     info jsonb NOT NULL DEFAULT '{}'::jsonb,
     remote_management boolean NOT NULL DEFAULT false,
     policy jsonb NOT NULL DEFAULT '{}'::jsonb,
     place text NOT NULL DEFAULT '',
     created_at timestamptz NOT NULL DEFAULT now(),
     last_seen timestamptz
   )`,
  `CREATE TABLE IF NOT EXISTS device_links (
     id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
     device_code_hash text NOT NULL UNIQUE,
     user_code text NOT NULL,
     info jsonb NOT NULL DEFAULT '{}'::jsonb,
     place text NOT NULL DEFAULT '',
     user_id uuid REFERENCES users(id) ON DELETE CASCADE,
     device_id uuid,
     created_at timestamptz NOT NULL DEFAULT now(),
     expires_at timestamptz NOT NULL
   )`,
  `CREATE INDEX IF NOT EXISTS device_links_code ON device_links (user_code)`,
  `CREATE TABLE IF NOT EXISTS commands (
     id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
     device_id uuid NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
     kind text NOT NULL,
     status text NOT NULL DEFAULT 'pending',
     detail text NOT NULL DEFAULT '',
     created_at timestamptz NOT NULL DEFAULT now(),
     updated_at timestamptz NOT NULL DEFAULT now()
   )`,
  `CREATE TABLE IF NOT EXISTS events (
     id bigserial PRIMARY KEY,
     user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
     kind text NOT NULL,
     detail jsonb NOT NULL DEFAULT '{}'::jsonb,
     place text NOT NULL DEFAULT '',
     at timestamptz NOT NULL DEFAULT now()
   )`,
  `CREATE INDEX IF NOT EXISTS events_user ON events (user_id, at DESC)`,
  `CREATE TABLE IF NOT EXISTS sync_items (
     user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
     key text NOT NULL,
     value jsonb NOT NULL,
     device_id uuid,
     updated_at timestamptz NOT NULL DEFAULT now(),
     PRIMARY KEY (user_id, key)
   )`,
  // one backup per computer (its settings, edition and apps), so a new computer can be set up the same way
  `CREATE TABLE IF NOT EXISTS device_backups (
     device_id uuid PRIMARY KEY REFERENCES devices(id) ON DELETE CASCADE,
     user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
     data jsonb NOT NULL,
     updated_at timestamptz NOT NULL DEFAULT now()
   )`,
  `CREATE TABLE IF NOT EXISTS email_tokens (
     token_hash text PRIMARY KEY,
     user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
     kind text NOT NULL,
     email text NOT NULL,
     expires_at timestamptz NOT NULL,
     used_at timestamptz
   )`,
  `CREATE TABLE IF NOT EXISTS attempts (
     key text NOT NULL,
     at timestamptz NOT NULL DEFAULT now()
   )`,
  `CREATE INDEX IF NOT EXISTS attempts_key ON attempts (key, at)`,
  `CREATE TABLE IF NOT EXISTS feedback (
     id bigserial PRIMARY KEY,
     email text NOT NULL DEFAULT '',
     topic text NOT NULL,
     message text NOT NULL,
     place text NOT NULL DEFAULT '',
     at timestamptz NOT NULL DEFAULT now()
   )`,
  // Paid online services, if Poly ever has them. Not used or shown anywhere yet.
  `CREATE TABLE IF NOT EXISTS subscriptions (
     id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
     user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
     product text NOT NULL,
     status text NOT NULL,
     started_at timestamptz NOT NULL DEFAULT now(),
     ends_at timestamptz
   )`,
];

async function migrate(conn) {
  for (const statement of SCHEMA) await conn.query(statement);
}
