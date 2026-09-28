-- ── YAHOO OAUTH TOKENS ────────────────────────────────────────────────
-- Server-only. No RLS policy grants anon/authenticated anything here -
-- the client never sees a raw Yahoo token; only Edge Functions (via the
-- service_role key, auto-injected into every Edge Function by Supabase)
-- ever read or write this table.
create table public.yahoo_oauth_tokens (
  user_id       uuid primary key references auth.users(id) on delete cascade,
  access_token  text not null,
  refresh_token text not null,
  expires_at    timestamptz not null,
  scope         text,
  updated_at    timestamptz not null default now()
);

alter table public.yahoo_oauth_tokens enable row level security;
-- Deliberately no policies - default-deny for anon/authenticated.


-- ── PROFILES: Yahoo connection status ────────────────────────────────
-- Client-visible connection STATUS only, never the tokens themselves.
-- profiles already has the right RLS shape (auth.uid() = id) and is
-- already the natural place fantasy.html reads its own account state
-- from, so this is a minimal addition rather than a new client-facing
-- table.
--
-- Known, accepted looseness: profiles' existing RLS ("for all using
-- auth.uid() = id") technically lets a signed-in user set their own
-- yahoo_connected flag directly without ever completing OAuth. Not
-- worth column-level RLS machinery for this - the flag is purely
-- cosmetic UI state; every real Yahoo API call an Edge Function ever
-- makes checks yahoo_oauth_tokens directly (service-role only,
-- unspoofable), never this flag.
alter table public.profiles add column yahoo_connected boolean not null default false;
alter table public.profiles add column yahoo_connected_at timestamptz;
