-- ── STRIPE CUSTOMER → EMAIL MAP ──────────────────────────────────────
-- Server-only. Written by supabase/functions/stripe-webhook on
-- checkout.session.completed (the one event whose payload actually
-- includes the customer's email), read back on
-- customer.subscription.created/.deleted - those two only ever carry a
-- Stripe customer ID, never an email, and this table is how the
-- function resolves one from the other without needing a Stripe API
-- key (STRIPE_SECRET_KEY) on top of the webhook signing secret it
-- already requires. No client ever reads or writes this; only the Edge
-- Function, via the service_role key.
create table public.stripe_customers (
  stripe_customer_id text primary key,
  email               text not null,
  created_at          timestamptz not null default now()
);

alter table public.stripe_customers enable row level security;
-- Deliberately no policies - default-deny for anon/authenticated, same
-- as yahoo_oauth_tokens.
