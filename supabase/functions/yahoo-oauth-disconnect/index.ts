// JWT-verified (project default) - only a real signed-in Supabase user
// can disconnect their OWN Yahoo connection. Deletes the stored tokens
// outright and clears the two profiles columns, the same cleanup path
// getValidYahooAccessToken() uses when a refresh token turns out to be
// dead - "disconnected" and "needs reconnect" are deliberately the same
// state from the client's perspective (see the plan's "Refresh" section).
//
// Not calling a Yahoo token-revocation endpoint here - Yahoo's OAuth2
// docs don't clearly document one the way Google's /revoke is
// documented, and this project's standing rule is to verify against the
// real docs (or a live call) before relying on an endpoint rather than
// guess one. Worth revisiting when this is actually being tested live.
import { createClient } from "jsr:@supabase/supabase-js@2";
import { corsHeaders } from "../_shared/cors.ts";
import { getAnonKey, getServiceRoleKey } from "../_shared/supabase_keys.ts";

const SUPABASE_URL = Deno.env.get("SUPABASE_URL")!;

function jsonResponse(body: unknown, status: number): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { ...corsHeaders, "Content-Type": "application/json" },
  });
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") {
    return new Response(null, { headers: corsHeaders });
  }

  try {
    const authHeader = req.headers.get("Authorization");
    if (!authHeader) return jsonResponse({ error: "missing Authorization header" }, 401);

    const supabase = createClient(SUPABASE_URL, getAnonKey(), {
      global: { headers: { Authorization: authHeader } },
    });
    const { data: userData, error: userError } = await supabase.auth.getUser();
    if (userError || !userData?.user) return jsonResponse({ error: "not signed in" }, 401);
    const userId = userData.user.id;

    const supabaseAdmin = createClient(SUPABASE_URL, getServiceRoleKey());
    const { error: deleteError } = await supabaseAdmin.from("yahoo_oauth_tokens").delete().eq("user_id", userId);
    if (deleteError) {
      console.error("yahoo_oauth_tokens delete error:", deleteError);
      return jsonResponse({ error: "failed to disconnect" }, 500);
    }

    const { error: profileError } = await supabaseAdmin
      .from("profiles")
      .update({ yahoo_connected: false, yahoo_connected_at: null })
      .eq("id", userId);
    if (profileError) {
      console.error("profiles yahoo_connected clear error (non-fatal):", profileError);
    }

    return jsonResponse({ ok: true }, 200);
  } catch (e) {
    console.error("yahoo-oauth-disconnect error:", e);
    return jsonResponse({ error: "internal error" }, 500);
  }
});
