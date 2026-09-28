// JWT-verified (project default) - only a real signed-in Supabase user
// can reach this. Client calls it via supabase.functions.invoke(), which
// attaches the user's session JWT automatically. Returns the Yahoo
// authorization URL to redirect the browser to; see the approved plan's
// "Flow, end to end" for the full sequence.
import { createClient } from "jsr:@supabase/supabase-js@2";
import { corsHeaders } from "../_shared/cors.ts";
import { buildAuthorizeUrl, signState } from "../_shared/yahoo.ts";

const SUPABASE_URL = Deno.env.get("SUPABASE_URL")!;
const SUPABASE_ANON_KEY = Deno.env.get("SUPABASE_ANON_KEY")!;
const YAHOO_CLIENT_ID = Deno.env.get("YAHOO_CLIENT_ID")!;
const YAHOO_REDIRECT_URI = Deno.env.get("YAHOO_REDIRECT_URI")!;
const YAHOO_STATE_SECRET = Deno.env.get("YAHOO_STATE_SECRET")!;

// Read-only fantasy scope - this connection only ever reads Yahoo data
// on the user's behalf, never acts on their league, matching the rest
// of this project's "display real data, never act for the user on an
// external platform" pattern (see the plan's "Out of scope" section).
const YAHOO_SCOPE = "fspt-r";

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

    // Resolves the real signed-in user from their own session JWT rather
    // than trusting anything client-supplied - this is what makes the
    // signed state trustworthy downstream in yahoo-oauth-callback.
    const supabase = createClient(SUPABASE_URL, SUPABASE_ANON_KEY, {
      global: { headers: { Authorization: authHeader } },
    });
    const { data: userData, error: userError } = await supabase.auth.getUser();
    if (userError || !userData?.user) return jsonResponse({ error: "not signed in" }, 401);

    const state = await signState(userData.user.id, YAHOO_STATE_SECRET);
    const authorizeUrl = buildAuthorizeUrl(YAHOO_CLIENT_ID, YAHOO_REDIRECT_URI, YAHOO_SCOPE, state);

    return jsonResponse({ authorize_url: authorizeUrl }, 200);
  } catch (e) {
    console.error("yahoo-oauth-start error:", e);
    return jsonResponse({ error: "internal error" }, 500);
  }
});
