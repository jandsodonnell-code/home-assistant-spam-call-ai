# Spam Call AI

This Home Assistant app answers inbound Twilio calls and bridges the caller's
audio to OpenAI GPT-Live.

## Version 0.3.0

Version 0.3.0 keeps the stable ngrok tunnel and adds automatic post-call AI analysis. After each call, a separate low-cost text model reviews the captured transcript and classifies the call as legitimate, telemarketing, scam, robocall, or unknown. It also creates a short summary and recommended action.

No router port forwarding is required because the ngrok agent establishes the
connection outbound from Home Assistant.

## Required configuration

### OpenAI API key

Use the API key created for this project. Keep it private.

### Twilio Account SID

This begins with `AC`.

### Twilio Auth Token

Use the Auth Token from the Twilio Console. Keep it private.

### ngrok Authtoken

Copy the ngrok authtoken from your ngrok dashboard and paste it into the app
configuration. Keep it private.

### ngrok Domain

For this installation the assigned development domain is:

`wand-mold-displace.ngrok-free.dev`

The public base URL is therefore:

`https://wand-mold-displace.ngrok-free.dev`

and the Twilio webhook is:

`https://wand-mold-displace.ngrok-free.dev/twiml`

## Tunnel selection

The app checks tunnel options in this order:

1. `public_base_url`, when explicitly configured.
2. ngrok, when both `ngrok_authtoken` and `ngrok_domain` are configured.
3. Cloudflare Quick Tunnel, only when `auto_tunnel: true`.

For the stable ngrok setup, keep `auto_tunnel: false`.

## Twilio webhook

Set the Twilio Voice incoming-call webhook to:

`https://wand-mold-displace.ngrok-free.dev/twiml`

and use HTTP POST.

## Home Assistant events

The app fires:

- `spam_call_ai_call_started`
- `spam_call_ai_call_ended`

The ended event includes caller number, call duration, call SID, and recent
caller/assistant transcript text.

## Useful endpoints

- `/health` - health/configuration status and public URL
- `/status` - public URL, Twilio webhook URL, active-call count, last call
- `/twiml` - Twilio inbound voice webhook
- `/media/<secret>` - private bidirectional Twilio Media Stream endpoint

## Security notes

- Never paste the ngrok authtoken, OpenAI API key, or Twilio Auth Token into
  chat messages or screenshots.
- Keep Twilio signature validation enabled except during controlled debugging.
- Use a dedicated OpenAI project/key and set a project spending limit.
- Use a maximum call duration.
- The voice model is not given Home Assistant control, email access, contacts,
  financial access, or other private tools.


## Call analysis

Call analysis is enabled by default:

- `analyze_calls: true`
- `analysis_model: gpt-6-luna`

After the voice call ends, the app sends the recent transcript to the OpenAI
Responses API using Structured Outputs. The analysis is saved with the last-call
record and emitted as the Home Assistant event:

`spam_call_ai_call_analyzed`

The event includes:

- classification
- spam likelihood from 0 to 1
- caller name, when stated
- organization, when stated
- reason for calling
- callback number, only when stated
- brief summary
- recommended action: allow, block, or review
- short factual signals used for screening

This stage does not automatically transfer or block calls yet. It lets us test
classification quality before allowing the AI to route real callers.
