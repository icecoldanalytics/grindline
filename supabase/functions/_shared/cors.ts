// Locked to the exact origin the site actually runs on - not "*", and
// not the apex. Confirmed live during the magic-link redirect fix that
// GitHub Pages 301s grindline.ca -> www.grindline.ca before any page JS
// executes, so a browser calling these functions is always doing so
// from https://www.grindline.ca, never the bare apex. Getting this
// wrong here would be the exact same class of silent failure as the
// emailRedirectTo bug (see fantasy.html's AUTH_REDIRECT_URL comment).
export const corsHeaders = {
  "Access-Control-Allow-Origin": "https://www.grindline.ca",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
};
