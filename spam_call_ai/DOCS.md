# Spam Call AI

This Home Assistant app answers inbound Twilio calls and bridges the caller's
audio to OpenAI GPT-Live.

## Version 0.1.1

Version 0.1.1 adds an automatic Cloudflare Quick Tunnel for initial testing.
This means you do not need to expose Home Assistant itself or configure a
separate tunnel before your first test.

Cloudflare Quick Tunnels are intended only for testing. Their public URL changes
whenever the app or tunnel restarts. After testing, switch to a stable named
tunnel or another stable HTTPS hostname.

## Required configuration

### OpenAI API key

Use the API key created for this project. Keep it private.

### Twilio Account SID

This begins with `AC`.

### Twilio Auth Token

Use the Auth Token from the Twilio Console. Keep it private.

## Public access

### Easiest testing setup

Leave:

- `auto_tunnel: true`
- `public_base_url` blank

When the app starts, it will create a free temporary
`https://....trycloudflare.com` URL and print two lines in the app log:

- `PUBLIC BASE URL: ...`
- `TWILIO WEBHOOK: .../twiml`

Use the TWILIO WEBHOOK value in Twilio.

The temporary URL changes if the app restarts, so you must update Twilio after
a restart during testing.

### Stable production setup

Later, set `auto_tunnel: false` and enter a stable HTTPS URL in
`public_base_url`. A named Cloudflare Tunnel is one option.

Do not point this app at your normal Home Assistant URL unless you have
explicitly configured a reverse proxy route for this app.

## Other options

### Maximum Call Minutes

Start with 10 minutes to control API usage.

### Voice

The default is `marin`, a GPT-Live built-in voice.

### Greeting

This is spoken by Twilio before the live AI audio stream starts.

## Twilio webhook

After the app is running:

- Incoming call method: Webhook
- Webhook URL: copy the `TWILIO WEBHOOK` line from the app log
- HTTP method: POST

The app validates Twilio's HTTP signature by default.

## Home Assistant events

The app fires:

- `spam_call_ai_call_started`
- `spam_call_ai_call_ended`

The ended event includes the caller number, call duration, call SID, and recent
caller/assistant transcript text.

## Useful endpoints

- `/health` - health/configuration status and public URL
- `/status` - public URL, Twilio webhook URL, active-call count, last call
- `/twiml` - Twilio inbound voice webhook
- `/media/<secret>` - private bidirectional Twilio Media Stream endpoint

## Security notes

- Never paste API keys or Auth Tokens into chat messages or screenshots.
- Keep Twilio signature validation enabled except during controlled debugging.
- Use a dedicated OpenAI project/key and set a project spending limit.
- Use a maximum call duration.
- The voice model is not given Home Assistant control, email access, contacts,
  financial access, or other private tools.

## Testing

Call the Twilio number from a verified caller if your Twilio account is still
on trial.

Try a legitimate-caller scenario and a fake telemarketing scenario. Do not use
real account numbers, passwords, verification codes, or financial details.
