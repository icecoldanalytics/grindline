// Shared by every Yahoo OAuth Edge Function (yahoo-oauth-start,
// yahoo-oauth-callback, yahoo-oauth-disconnect, and eventually whatever
// Edge Function first actually reads live Yahoo Fantasy data) - state
// signing/verification, the token exchange and refresh calls, and the
// lazy-refresh helper that's the single seam every future Yahoo-data
// caller goes through. See the approved plan (Yahoo Fantasy OAuth) for
// the full design and why each piece is shaped this way.

import type { SupabaseClient } from "jsr:@supabase/supabase-js@2";

const YAHOO_AUTHORIZE_URL = "https://api.login.yahoo.com/oauth2/request_auth";
const YAHOO_TOKEN_URL = "https://api.login.yahoo.com/oauth2/get_token";

// ── base64url (no external import - Deno's global btoa/atob plus a
// manual charset swap is all this needs) ──────────────────────────────
function toBase64Url(bytes: Uint8Array): string {
  let binary = "";
  for (const b of bytes) binary += String.fromCharCode(b);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function fromBase64Url(s: string): Uint8Array {
  const b64 = s.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - (s.length % 4)) % 4);
  const binary = atob(b64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

// ── HMAC-SHA256 via Deno's built-in Web Crypto (crypto.subtle) ────────
async function hmacKey(secret: string): Promise<CryptoKey> {
  return crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign", "verify"],
  );
}

// ── Signed, stateless `state` - see the plan's "Why a signed state, not
// a state table" section. No database round trip, no cleanup job; the
// signature plus a 600s freshness window does the whole job. ──────────
const STATE_TTL_SECONDS = 600; // 10 minutes - covers a normal Yahoo login/2FA/consent pause

export async function signState(userId: string, secret: string): Promise<string> {
  const issuedAt = Math.floor(Date.now() / 1000).toString();
  const payload = `${userId}.${issuedAt}`;
  const key = await hmacKey(secret);
  const sig = new Uint8Array(await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(payload)));
  return `${toBase64Url(new TextEncoder().encode(payload))}.${toBase64Url(sig)}`;
}

export async function verifyState(state: string, secret: string): Promise<{ userId: string } | null> {
  const parts = state.split(".");
  if (parts.length !== 2) return null;
  const [payloadB64, sigB64] = parts;

  let payload: string;
  try {
    payload = new TextDecoder().decode(fromBase64Url(payloadB64));
  } catch {
    return null;
  }

  let sig: Uint8Array;
  try {
    sig = fromBase64Url(sigB64);
  } catch {
    return null;
  }

  const key = await hmacKey(secret);
  // crypto.subtle.verify does a constant-time comparison internally -
  // deliberately used instead of recomputing the signature and comparing
  // strings by hand, which would need its own timing-safe compare to be
  // correct.
  const ok = await crypto.subtle.verify("HMAC", key, sig, new TextEncoder().encode(payload)).catch(() => false);
  if (!ok) return null;

  const [userId, issuedAtStr] = payload.split(".");
  if (!userId || !issuedAtStr) return null;
  const issuedAt = parseInt(issuedAtStr, 10);
  if (!Number.isFinite(issuedAt)) return null;

  const ageSeconds = Math.floor(Date.now() / 1000) - issuedAt;
  // A small negative allowance covers minor clock skew between the
  // function instance that signed it and the one verifying it.
  if (ageSeconds > STATE_TTL_SECONDS || ageSeconds < -30) return null;

  return { userId };
}

export function buildAuthorizeUrl(clientId: string, redirectUri: string, scope: string, state: string): string {
  const params = new URLSearchParams({
    client_id: clientId,
    redirect_uri: redirectUri,
    response_type: "code",
    scope,
    state,
  });
  return `${YAHOO_AUTHORIZE_URL}?${params.toString()}`;
}

// ── Token exchange / refresh ───────────────────────────────────────────
export interface YahooTokenResponse {
  access_token: string;
  refresh_token: string;
  expires_in: number;
  token_type: string;
  scope?: string;
}

export class YahooReauthRequiredError extends Error {
  constructor(message = "Yahoo refresh token is no longer valid - user must reconnect") {
    super(message);
    this.name = "YahooReauthRequiredError";
  }
}

interface YahooHttpError extends Error {
  status?: number;
}

// Yahoo's documented token-endpoint auth style is HTTP Basic with
// client_id:client_secret - worth reconfirming against Yahoo's current
// developer docs during the first real live test rather than trusting
// this unverified, same standard this project has held since the
// magic-link investigation ("verify, don't guess").
async function postYahooToken(body: URLSearchParams, clientId: string, clientSecret: string): Promise<YahooTokenResponse> {
  const basicAuth = btoa(`${clientId}:${clientSecret}`);
  const r = await fetch(YAHOO_TOKEN_URL, {
    method: "POST",
    headers: {
      "Content-Type": "application/x-www-form-urlencoded",
      "Authorization": `Basic ${basicAuth}`,
    },
    body: body.toString(),
  });
  if (!r.ok) {
    const text = await r.text().catch(() => "");
    const err = new Error(`Yahoo token endpoint returned ${r.status}: ${text}`) as YahooHttpError;
    err.status = r.status;
    throw err;
  }
  return r.json();
}

export async function exchangeCodeForTokens(
  code: string,
  clientId: string,
  clientSecret: string,
  redirectUri: string,
): Promise<YahooTokenResponse> {
  const body = new URLSearchParams({
    grant_type: "authorization_code",
    code,
    redirect_uri: redirectUri,
  });
  return postYahooToken(body, clientId, clientSecret);
}

async function refreshYahooToken(
  refreshToken: string,
  clientId: string,
  clientSecret: string,
): Promise<YahooTokenResponse> {
  const body = new URLSearchParams({
    grant_type: "refresh_token",
    refresh_token: refreshToken,
  });
  return postYahooToken(body, clientId, clientSecret);
}

// ── The one seam every Yahoo-data caller goes through ──────────────────
// Reads the stored token, returns it if still fresh, otherwise refreshes
// it first. Distinguishes a TRANSIENT refresh failure (Yahoo 5xx,
// timeout, network error - stored tokens are untouched, they may still
// be perfectly good) from a DEFINITIVE one (Yahoo 400/401 - invalid_grant
// is what Yahoo sends for a dead or revoked refresh token, which will
// never succeed no matter how many times it's retried). Only the
// definitive case clears the connection: deletes the token row and
// flips profiles.yahoo_connected back to false, then throws
// YahooReauthRequiredError specifically so callers can show "reconnect"
// instead of a generic error or, worse, silently-stale data. See the
// plan's "Refresh" section for the full reasoning, including why this
// is lazy/on-demand rather than a scheduled job.
export async function getValidYahooAccessToken(
  supabaseAdmin: SupabaseClient,
  userId: string,
  clientId: string,
  clientSecret: string,
): Promise<string> {
  const { data: row, error } = await supabaseAdmin
    .from("yahoo_oauth_tokens")
    .select("access_token, refresh_token, expires_at")
    .eq("user_id", userId)
    .maybeSingle();

  if (error) throw new Error(`yahoo_oauth_tokens read error: ${error.message}`);
  if (!row) throw new YahooReauthRequiredError("No Yahoo connection on file");

  const expiresAtMs = new Date(row.expires_at).getTime();
  if (expiresAtMs > Date.now() + 60_000) {
    return row.access_token;
  }

  let refreshed: YahooTokenResponse;
  try {
    refreshed = await refreshYahooToken(row.refresh_token, clientId, clientSecret);
  } catch (e) {
    const status = (e as YahooHttpError)?.status;
    if (status === 400 || status === 401) {
      await supabaseAdmin.from("yahoo_oauth_tokens").delete().eq("user_id", userId);
      await supabaseAdmin.from("profiles").update({ yahoo_connected: false }).eq("id", userId);
      throw new YahooReauthRequiredError();
    }
    throw new Error(`Yahoo token refresh failed transiently: ${(e as Error)?.message ?? e}`);
  }

  const newExpiresAt = new Date(Date.now() + refreshed.expires_in * 1000).toISOString();
  await supabaseAdmin
    .from("yahoo_oauth_tokens")
    .update({
      access_token: refreshed.access_token,
      // Some providers rotate the refresh token on every use; trust
      // whichever fields Yahoo's response actually includes and keep
      // the stored value for whichever it omits.
      refresh_token: refreshed.refresh_token ?? row.refresh_token,
      expires_at: newExpiresAt,
      updated_at: new Date().toISOString(),
    })
    .eq("user_id", userId);

  return refreshed.access_token;
}
