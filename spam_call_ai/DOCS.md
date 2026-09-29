# Spam Call AI

This Home Assistant app answers inbound Twilio calls and bridges the caller's
audio to OpenAI GPT-Live.

## Version 0.1.0

This first version is intentionally isolated:

- Every call to the Twilio number is answered by the automated assistant.
- It does not transfer legitimate callers yet.
- It does not place outbound calls.
- It does not handle SMS yet.
- A maximum call length is enforced.
- The app fires Home Assistant events when a call starts and ends.

## Required configuration

### OpenAI API key

Use the API key created for this project. Keep it private.

### Twilio Account SID

This begins with `AC`.

### Twilio Auth Token

Use the Auth Token from the Twilio Console. Keep it private.

A Twilio API Key SID/Secret is not required by version 0.1.0 because this
version only receives Twilio webhooks and Media Streams.

### Public Base URL

The app must be reachable by Twilio over public HTTPS/WSS.

Example:

`https://spam-ai.example.com`

Do not include `/twiml` or `/media` in this setting.

For initial testing, expose only this app's port 8000 through a secure tunnel.
Do not expose the entire Home Assistant interface just for this app.

### Maximum Call Minutes

Start with 10 minutes to control API usage.

### Voice

The default is `gleam`. You can enter another GPT-Live built-in voice ID.

## Twilio webhook

After the app is running and the public URL works, configure the Twilio number:

- Incoming call method: Webhook
- Webhook URL: `https://YOUR-PUBLIC-HOST/twiml`
- HTTP method: POST

The app validates Twilio's HTTP signature by default.

## Home Assistant events

The app fires:

- `spam_call_ai_call_started`
- `spam_call_ai_call_ended`

The ended event includes the caller number, call duration, call SID, and recent
caller/assistant transcript text.

## Useful endpoints

- `/health` - health and configuration status
- `/status` - active-call count and last-call data
- `/twiml` - Twilio inbound voice webhook
- `/media/<secret>` - private bidirectional Twilio Media Stream endpoint

The media-path secret is derived from your Twilio Auth Token and is not shown
in the Home Assistant UI.

## Security notes

- Never paste API keys or Auth Tokens into chat messages or screenshots.
- Keep Twilio signature validation enabled except during controlled debugging.
- Use a dedicated OpenAI project/key and set a project spending limit.
- Use a maximum call duration.
- This app never intentionally gives the voice model Home Assistant control,
  email access, contacts, financial access, or other private tools.

## Testing

Call the Twilio number from a verified caller if your Twilio account is still
on trial.

Try a legitimate-caller scenario and a fake telemarketing scenario. Do not use
real account numbers, passwords, verification codes, or financial details in
tests.
