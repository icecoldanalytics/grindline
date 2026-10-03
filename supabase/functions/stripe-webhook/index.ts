// PUBLIC function - must be deployed with JWT verification OFF (CLI:
// `supabase functions deploy stripe-webhook --no-verify-jwt`, or the
// [functions.stripe-webhook] verify_jwt = false entry in
// supabase/config.toml, included alongside this function). Stripe's
// webhook delivery is server-to-server with no Supabase Authorization
// header attached - it cannot pass Supabase's own JWT gate, and
// doesn't need to: this function's security comes entirely from
// verifying the Stripe-Signature header against STRIPE_WEBHOOK_SECRET
// (constructEventAsync below), not from a session.
//
// No _shared/cors.ts here, unlike the Yahoo functions - this is only
// ever called from Stripe's servers, never a browser, so CORS doesn't
// apply.
//
// Returns non-2xx on failure on purpose: Stripe automatically retries
// a webhook delivery that doesn't get a 2xx response, with backoff -
// free resilience against a transient Brevo/Supabase error that we
// don't have to build ourselves. Every write this function makes
// (Brevo's add/remove, the stripe_customers upsert) is idempotent, so
// a retried delivery is always safe to replay. 400 specifically for a
// signature that fails verification, per Stripe's own documented
// convention (tells Stripe not to bother retrying a request that will
// never pass); 500 for anything else.
//
// Resolving an email for customer.subscription.created/.deleted:
// neither event's payload includes the customer's email, only a
// Stripe customer ID - normally that means an extra Stripe API call
// (and a STRIPE_SECRET_KEY on top of the webhook signing secret this
// function already needs). Instead, checkout.session.completed (which
// does carry the email) upserts (customer_id, email) into
// stripe_customers, and the other two events read it back from there.
// Keeps this function to exactly the two secrets it was scoped for.
// See supabase_schema_stripe.sql for the table itself.
import Stripe from "npm:stripe@17";
import { createClient, type SupabaseClient } from "jsr:@supabase/supabase-js@2";
import { getServiceRoleKey } from "../_shared/supabase_keys.ts";

const SUPABASE_URL = Deno.env.get("SUPABASE_URL")!;
const STRIPE_WEBHOOK_SECRET = Deno.env.get("STRIPE_WEBHOOK_SECRET")!;
const BREVO_API_KEY = Deno.env.get("BREVO_API_KEY")!;

const BREVO_LIST_ID_RAW = Deno.env.get("BREVO_LIST_ID");
if (!BREVO_LIST_ID_RAW) {
  throw new Error("BREVO_LIST_ID is not set");
}
const BREVO_LIST_ID = Number(BREVO_LIST_ID_RAW);

// The API key argument is never used - constructEventAsync() below does
// pure local HMAC verification against STRIPE_WEBHOOK_SECRET, no network
// call, so no real Stripe secret key exists or is needed here.
// createFetchHttpClient() is required in Deno regardless, since the
// SDK's default HTTP client assumes Node's http module.
const stripe = new Stripe("sk_unused_webhook_signature_verification_only", {
  apiVersion: "2024-06-20",
  httpClient: Stripe.createFetchHttpClient(),
});

async function brevoAddToList(email: string): Promise<void> {
  const r = await fetch("https://api.brevo.com/v3/contacts", {
    method: "POST",
    headers: { "api-key": BREVO_API_KEY, "content-type": "application/json" },
    // updateEnabled: true is what makes this idempotent against Stripe's
    // at-least-once webhook delivery - a redelivered event just re-adds
    // the same contact to the same list instead of Brevo erroring on
    // "contact already exists".
    body: JSON.stringify({ email, listIds: [BREVO_LIST_ID], updateEnabled: true }),
  });
  if (!r.ok) {
    throw new Error(`Brevo add-to-list failed: HTTP ${r.status} - ${await r.text()}`);
  }
}

async function brevoRemoveFromList(email: string): Promise<void> {
  // Removes them from this list specifically, not a full contact
  // delete - they may be on other lists or have other data worth
  // keeping.
  const r = await fetch(`https://api.brevo.com/v3/contacts/${encodeURIComponent(email)}`, {
    method: "PUT",
    headers: { "api-key": BREVO_API_KEY, "content-type": "application/json" },
    body: JSON.stringify({ unlinkListIds: [BREVO_LIST_ID] }),
  });
  if (!r.ok) {
    throw new Error(`Brevo remove-from-list failed: HTTP ${r.status} - ${await r.text()}`);
  }
}

async function lookupEmail(supabaseAdmin: SupabaseClient, customerId: string): Promise<string | null> {
  const { data, error } = await supabaseAdmin
    .from("stripe_customers")
    .select("email")
    .eq("stripe_customer_id", customerId)
    .maybeSingle();
  if (error) {
    console.error(`stripe-webhook: stripe_customers lookup error for ${customerId}:`, error);
    return null;
  }
  return data?.email ?? null;
}

Deno.serve(async (req) => {
  if (req.method !== "POST") {
    return new Response("method not allowed", { status: 405 });
  }

  const signature = req.headers.get("stripe-signature");
  // Raw text, read BEFORE any JSON parsing - the signature is computed
  // over the exact raw bytes Stripe sent; parsing and re-stringifying
  // would produce different bytes and break verification.
  const rawBody = await req.text();
  if (!signature) {
    console.error("stripe-webhook: missing Stripe-Signature header");
    return new Response("missing signature", { status: 400 });
  }

  let event: Stripe.Event;
  try {
    event = await stripe.webhooks.constructEventAsync(rawBody, signature, STRIPE_WEBHOOK_SECRET);
  } catch (e) {
    console.error("stripe-webhook: signature verification failed:", e);
    return new Response("invalid signature", { status: 400 });
  }

  try {
    // getServiceRoleKey() can throw (no usable key found at all) -
    // inside this try so a misconfigured key logs clearly and returns
    // 500 (Stripe retries) instead of Deno's own generic error page.
    const supabaseAdmin = createClient(SUPABASE_URL, getServiceRoleKey());

    switch (event.type) {
      case "checkout.session.completed": {
        const session = event.data.object as Stripe.Checkout.Session;
        if (session.mode !== "subscription") {
          // Defensive only - this Payment Link is subscription-mode and
          // it's the only thing this Stripe account currently sells,
          // but a one-off future product shouldn't silently add buyers
          // to the Rest Edge list.
          console.log(`stripe-webhook: ignoring checkout.session.completed in mode=${session.mode}`);
          break;
        }
        const email = session.customer_details?.email;
        const customerId = typeof session.customer === "string" ? session.customer : session.customer?.id;
        if (!email || !customerId) {
          console.error("stripe-webhook: checkout.session.completed missing email or customer id - not retrying, redelivery won't change this", { email, customerId });
          break;
        }
        const { error: upsertError } = await supabaseAdmin
          .from("stripe_customers")
          .upsert({ stripe_customer_id: customerId, email });
        if (upsertError) {
          // Thrown (not just logged) so the outer catch returns 500 and
          // Stripe retries - both this upsert and brevoAddToList below
          // are idempotent, so a retry is safe, and the mapping needs
          // to land for customer.subscription.deleted to ever be able
          // to remove this person later.
          throw new Error(`stripe_customers upsert failed: ${upsertError.message}`);
        }
        await brevoAddToList(email);
        console.log(`stripe-webhook: added ${email} to Brevo list ${BREVO_LIST_ID} (checkout.session.completed)`);
        break;
      }

      case "customer.subscription.created": {
        const sub = event.data.object as Stripe.Subscription;
        const customerId = typeof sub.customer === "string" ? sub.customer : sub.customer.id;
        const email = await lookupEmail(supabaseAdmin, customerId);
        if (!email) {
          // Self-healing, not an error: Stripe doesn't guarantee
          // delivery order between this event and
          // checkout.session.completed for the same signup. If this
          // one arrives first, checkout.session.completed will still
          // populate stripe_customers and add them to Brevo moments
          // later via its own event.
          console.log(`stripe-webhook: no email on file yet for customer ${customerId} - checkout.session.completed should resolve this shortly`);
          break;
        }
        await brevoAddToList(email);
        console.log(`stripe-webhook: added ${email} to Brevo list ${BREVO_LIST_ID} (customer.subscription.created)`);
        break;
      }

      case "customer.subscription.deleted": {
        const sub = event.data.object as Stripe.Subscription;
        const customerId = typeof sub.customer === "string" ? sub.customer : sub.customer.id;
        const email = await lookupEmail(supabaseAdmin, customerId);
        if (!email) {
          // Not retried (returns success below, not 500) - if the
          // mapping was never stored, redelivery won't produce one.
          // Logged as an error, not just a log line, since this is the
          // one case that can leave a canceled subscriber stuck on the
          // list with no automatic path to remove them - may need
          // manual cleanup in Brevo.
          console.error(`stripe-webhook: can't resolve email for customer ${customerId} - cannot remove from Brevo, may need manual cleanup`);
          break;
        }
        await brevoRemoveFromList(email);
        console.log(`stripe-webhook: removed ${email} from Brevo list ${BREVO_LIST_ID} (customer.subscription.deleted)`);
        break;
      }

      default:
        // Stripe can be configured to send more event types than this
        // webhook asked for (e.g. "send all events" instead of
        // selecting specific ones) - ignore anything unrecognized
        // rather than erroring, since an unhandled event type isn't a
        // failure on this function's part.
        console.log(`stripe-webhook: ignoring unhandled event type ${event.type}`);
    }
  } catch (e) {
    console.error(`stripe-webhook: error handling ${event.type}:`, e);
    return new Response("internal error", { status: 500 });
  }

  return new Response("ok", { status: 200 });
});
