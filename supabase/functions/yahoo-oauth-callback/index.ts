// PUBLIC function - must be deployed with JWT verification OFF (CLI:
// `supabase functions deploy yahoo-oauth-callback --no-verify-jwt`, or
// the [functions.yahoo-oauth-callback] verify_jwt = false entry in
// supabase/config.toml, included alongside this function). Yahoo's
// redirect is a plain browser navigation with no Supabase Authorization
// header attached - it cannot pass Supabase's own JWT gate, and doesn't
// need to: this function's security comes entirely from validating
// `state` (see _shared/yahoo.ts's verifyState), not from a session.
//
// Getting the verify_jwt=false deploy step wrong is the single most
// likely way this whole flow silently breaks - Supabase's gateway would
// reject Yahoo's redirect with its own 401 before this code ever runs,
// and that failure happens entirely outside this function's own error
// handling / logging.
import { createClient } from "jsr:@supabase/supabase-js@2";
import { getServiceRoleKey } from "../_shared/supabase_keys.ts";
import { exchangeCodeForTokens, verifyState } from "../_shared/yahoo.ts";

const SUPABASE_URL = Deno.env.get("SUPABASE_URL")!;
const YAHOO_CLIENT_ID = Deno.env.get("YAHOO_CLIENT_ID")!;
const YAHOO_CLIENT_SECRET = Deno.env.get("YAHOO_CLIENT_SECRET")!;
const YAHOO_REDIRECT_URI = Deno.env.get("YAHOO_REDIRECT_URI")!;
const YAHOO_STATE_SECRET = Deno.env.get("YAHOO_STATE_SECRET")!;

// www, not the apex - confirmed live (magic-link fix) that grindline.ca
// 301s to www.grindline.ca, so a hardcoded apex URL here would silently
// repeat that exact bug.
const RETURN_URL = "https://www.grindline.ca/fantasy.html";

function redirectTo(param: string): Response {
  return new Response(null, {
    status: 302,
    headers: { Location: `${RETURN_URL}?yahoo=${param}` },
  });
}

Deno.serve(async (req) => {
  const url = new URL(req.url);
  const code = url.searchParams.get("code");
  const state = url.searchParams.get("state");
  const yahooError = url.searchParams.get("error");

  if (yahooError) {
    console.error("Yahoo returned an authorization error:", yahooError);
    return redirectTo("error=denied");
  }
  if (!code || !state) {
    return redirectTo("error=missing_params");
  }

  const verified = await verifyState(state, YAHOO_STATE_SECRET);
  if (!verified) {
    // Covers both a bad/tampered signature and a state older than the
    // 600s TTL - both are indistinguishable to the caller on purpose,
    // and both resolve the same way: try connecting again.
    return redirectTo("error=state_expired");
  }
  const { userId } = verified;

  let tokens;
  try {
    tokens = await exchangeCodeForTokens(code, YAHOO_CLIENT_ID, YAHOO_CLIENT_SECRET, YAHOO_REDIRECT_URI);
  } catch (e) {
    console.error("Yahoo code exchange failed:", e);
    return redirectTo("error=exchange_failed");
  }

  // getServiceRoleKey() can throw (no usable key found at all) - caught
  // here rather than left to crash the whole request with Deno's own
  // generic error page. This exact failure mode (an admin client built
  // with an undefined key from the old SUPABASE_SERVICE_ROLE_KEY var,
  // which stopped being reliably injected) is what previously surfaced
  // as every real connect attempt failing with error=storage_failed.
  let supabaseAdmin;
  try {
    supabaseAdmin = createClient(SUPABASE_URL, getServiceRoleKey());
  } catch (e) {
    console.error("could not resolve a service-role key:", e);
    return redirectTo("error=storage_failed");
  }

  const expiresAt = new Date(Date.now() + tokens.expires_in * 1000).toISOString();

  const { error: upsertError } = await supabaseAdmin.from("yahoo_oauth_tokens").upsert({
    user_id: userId,
    access_token: tokens.access_token,
    refresh_token: tokens.refresh_token,
    expires_at: expiresAt,
    scope: tokens.scope ?? null,
    updated_at: new Date().toISOString(),
  });
  if (upsertError) {
    console.error("yahoo_oauth_tokens upsert error:", upsertError);
    return redirectTo("error=storage_failed");
  }

  const { error: profileError } = await supabaseAdmin
    .from("profiles")
    .update({ yahoo_connected: true, yahoo_connected_at: new Date().toISOString() })
    .eq("id", userId);
  if (profileError) {
    // Tokens are already stored successfully at this point - a failure
    // to flip this purely cosmetic status flag shouldn't fail the whole
    // connect from the user's perspective. Logged, not surfaced.
    console.error("profiles yahoo_connected update error (non-fatal):", profileError);
  }

  return redirectTo("connected");
});
