# Spam Call AI

This Home Assistant app answers inbound Twilio calls and bridges the caller's
audio to OpenAI GPT-Live.

## Version 0.4.1

Version 0.4.1 makes the live-transfer trigger explicit for callers who ask for any person by name or ask to be connected, and requires the delegated backend to return a transfer-decision tool call so the app can enforce the transfer rules. Transfer is disabled by default. When enabled, GPT-Live may delegate a transfer decision to the Responses backend. The backend can request a transfer only when the caller explicitly asks to speak with the owner and the call appears clearly legitimate. The server independently enforces the confidence threshold before asking Twilio to redirect the active call to a <Dial> transfer.

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


## Live transfer

Live transfer is OFF by default.

To enable it:

1. In the app Configuration page, turn on **Show unused optional configuration options**.
2. Enter `forward_to_number` in E.164 format, for example `+13035551212`.
3. Set `live_transfer_enabled: true`.
4. Keep `transfer_min_confidence` at `0.92` initially.
5. Leave `transfer_timeout_seconds` at `25` unless you want a different ring time.
6. Save and restart the app.

The transfer destination is stored only in the Home Assistant app configuration.
You do not need to share the phone number in chat.

The called phone should normally see the original inbound caller ID because Twilio
is dialing a second party from the active inbound call.

### Transfer rules

A transfer is attempted only when the backend requests `transfer_to_owner` and
the app independently confirms:

- live transfer is enabled
- a destination number exists
- the caller was classified `legitimate`
- confidence is at or above the configured threshold
- the caller explicitly requested a transfer
- a specific reason for the call was supplied

Uncertain calls continue with the AI and should be handled as message-taking or
spam screening rather than transferred.

Home Assistant events:

- `spam_call_ai_transfer_requested`
- `spam_call_ai_transfer_started`
- `spam_call_ai_transfer_denied`
- `spam_call_ai_transfer_failed`


### Transfer diagnostics

When a caller asks to speak with someone, the log should now show:

- `Live transfer check delegated`
- `Transfer decision tool requested by backend`
- then either `Transfer started` or `Transfer denied`

This makes it clear whether the live model delegated the request and whether the
server accepted the backend decision.
