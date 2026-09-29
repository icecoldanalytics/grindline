// Resolves the Supabase-platform keys every Edge Function needs
// (service-role-equivalent for admin access, anon-equivalent for
// verifying a caller's own session) - deliberately separate from
// yahoo.ts since this is a Supabase-platform concern, not a Yahoo one,
// and any future Edge Function this project adds will need the same
// resolution.
//
// Confirmed against Supabase's own docs (2026-09-29): SUPABASE_ANON_KEY
// and SUPABASE_SERVICE_ROLE_KEY - single strings, auto-injected into
// every function - are now legacy/deprecated. The current variables are
// SUPABASE_PUBLISHABLE_KEYS and SUPABASE_SECRET_KEYS, and despite being
// plural they are NOT lists - each is a JSON dictionary keyed by key
// name, e.g. {"default": "sb_secret_..."}, read as
// JSON.parse(Deno.env.get('SUPABASE_SECRET_KEYS')!)['default'].
//
// Confirmed live on this project: yahoo-oauth-callback's token upsert
// was failing (storage_failed) because SUPABASE_SERVICE_ROLE_KEY is no
// longer being reliably injected here - the admin client was silently
// built with an empty/undefined key. New vars are tried first; the
// legacy single-string vars are kept as a fallback in case a future
// project (or this one, if Supabase's rollout changes again) still
// injects them - cheap to keep, and means this doesn't have to be
// touched again if the platform reverts or takes time fully cutting
// the legacy vars over.

function resolveFromJsonDict(envVarName: string): string | null {
  const raw = Deno.env.get(envVarName);
  if (!raw) return null;
  try {
    const dict = JSON.parse(raw);
    return dict?.default ?? null;
  } catch (e) {
    console.error(`${envVarName} is set but not valid JSON:`, e);
    return null;
  }
}

export function getServiceRoleKey(): string {
  const fromNew = resolveFromJsonDict("SUPABASE_SECRET_KEYS");
  if (fromNew) return fromNew;
  const legacy = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY");
  if (legacy) return legacy;
  throw new Error(
    "No service-role-equivalent key found (checked SUPABASE_SECRET_KEYS and the legacy SUPABASE_SERVICE_ROLE_KEY)",
  );
}

export function getAnonKey(): string {
  const fromNew = resolveFromJsonDict("SUPABASE_PUBLISHABLE_KEYS");
  if (fromNew) return fromNew;
  const legacy = Deno.env.get("SUPABASE_ANON_KEY");
  if (legacy) return legacy;
  throw new Error(
    "No anon-equivalent key found (checked SUPABASE_PUBLISHABLE_KEYS and the legacy SUPABASE_ANON_KEY)",
  );
}
