# Spam Call AI

This Home Assistant app answers inbound Twilio calls and bridges the caller's
audio to OpenAI GPT-Live.

## Version 0.8.0

Version 0.7.0 treats trusted contacts as true safe callers. Safe callers are matched before any OpenAI media stream is opened. They are re-rung to the cell once, and if that second attempt is not answered, Twilio records a normal voicemail instead of sending the caller to AI. Transfer is disabled by default. When enabled, GPT-Live may delegate a transfer decision to the Responses backend. The backend can request a transfer only when the caller explicitly asks to speak with the owner and the call appears clearly legitimate. The server independently enforces the confidence threshold before asking Twilio to redirect the active call to a <Dial> transfer.

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


## Trusted family re-ring

This mode is designed for conditional forwarding from the user's cell to the
Twilio number.

Configuration:

- `trusted_rering_enabled: true`
- `trusted_callers`: comma-separated trusted phone numbers
- `forward_to_number`: the user's cell number
- `trusted_rering_timeout_seconds: 15`

Enter trusted numbers in Home Assistant only; they do not need to be shared in
chat. Use E.164 format when possible, for example `+13035551212`.

Flow:

1. The cell rings normally.
2. If unanswered/rejected/unreachable, the carrier forwards the call to Twilio.
3. If the incoming caller number is in `trusted_callers`, Twilio rings the
   user's cell one more time.
4. The second ring intentionally shows the Twilio number as caller ID. This
   allows the app to detect a second unanswered call that is forwarded back to
   Twilio and stop the loop.
5. If the second ring is not answered, the original trusted caller falls back
   to the AI, which can take a message.
6. Non-trusted callers go directly to the AI message-taking/screening flow.

The app emits `spam_call_ai_trusted_caller_rering` when it recognizes a
trusted caller and starts the second ring.


## Google Contacts automatic sync

### Home Assistant configuration

Set:

- `google_contacts_enabled: true`
- `google_oauth_client_id`: your Google OAuth Web application client ID
- `google_oauth_client_secret`: your Google OAuth Web application client secret
- `google_setup_key`: create your own private setup password
- `google_contact_label: Trusted Callers`
- `google_contacts_sync_minutes: 15`

The OAuth client secret and setup key stay in Home Assistant app configuration.
Do not paste them into chat or screenshots.

### Google Cloud setup

1. Create or select a Google Cloud project.
2. Enable the **Google People API**.
3. Configure the Google Auth Platform / OAuth consent screen.
   For a normal personal Gmail account, use an External audience and add the
   Google account as a test user while the app is in Testing.
4. Create an OAuth client with application type **Web application**.
5. Add this exact Authorized redirect URI:

   `https://wand-mold-displace.ngrok-free.dev/google/oauth/callback`

6. Copy the Client ID and Client Secret into the Spam Call AI configuration.
7. Save and restart Spam Call AI.
8. Open:

   `https://wand-mold-displace.ngrok-free.dev/google/setup`

9. Enter the private Google setup key and choose **Authorize Google Contacts**.
10. Sign in to the Google account that owns the trusted contacts and approve the
    read-only Contacts permission.

The app stores the Google refresh token under the app's private /data directory
and refreshes the access token automatically.

### Add trusted contacts

Create a Google Contacts label named exactly:

`Trusted Callers`

On the Google Contacts website, select the people you want trusted, click
**Manage labels**, select **Trusted Callers**, and click **Apply**.

On Android Contacts, create/select the **Trusted Callers** label and add the
contacts to it.

The app automatically re-syncs the label every 15 minutes by default. The
Google setup page also includes a **Sync Trusted Callers** button for immediate
syncing.

Only contacts in that label are treated as Google-synced trusted callers.
Removing a person from the label removes them from the synced trusted list at
the next successful sync. Any phone numbers still present in the manual
`trusted_callers` configuration remain trusted.

### Events

Successful sync fires:

`spam_call_ai_google_contacts_synced`

The event includes the label, contact count, and phone-number count.


## Safe callers in 0.7.0

Any number in the Google Contacts label configured by `google_contact_label`
(default: `Trusted Callers`) or in the manual `trusted_callers` setting is safe.

Safe-call flow:

1. The normal cell rings first.
2. Conditional forwarding sends an unanswered call to Twilio.
3. The app checks the caller number against the safe list before creating any
   OpenAI connection.
4. Safe callers re-ring the cell once.
5. If the second ring is unanswered, Twilio records a conventional voicemail.
6. Safe callers never enter the AI screening stream.

The app creates this Home Assistant entity:

`sensor.spam_call_ai_safe_contacts`

Its state is the number of safe phone numbers. Its attributes include the safe
Google Contact names, the Google label, sync counts, last-sync timestamp, and
sync error if one exists. This is intended for the dashboard card.

The safe-call events are:

- `spam_call_ai_safe_caller`
- `spam_call_ai_safe_message_recorded`

The safe voicemail maximum length is controlled by
`safe_voicemail_max_seconds` and defaults to 120 seconds.


## Dashboard entities in 0.8.0

Version 0.8.0 publishes Home Assistant entities for the Spam Call AI dashboard.
They are refreshed automatically while the app is running.

- `binary_sensor.spam_call_ai_online`
- `sensor.spam_call_ai_active_calls`
- `sensor.spam_call_ai_safe_contacts`
- `sensor.spam_call_ai_last_call`
- `sensor.spam_call_ai_last_caller`
- `sensor.spam_call_ai_last_caller_name`
- `sensor.spam_call_ai_last_call_type`
- `sensor.spam_call_ai_last_classification`
- `sensor.spam_call_ai_last_spam_likelihood`
- `sensor.spam_call_ai_last_action`
- `sensor.spam_call_ai_last_duration`
- `sensor.spam_call_ai_last_summary`
- `sensor.spam_call_ai_last_callback_number`
- `sensor.spam_call_ai_last_call_time`
- `sensor.spam_call_ai_google_sync_status`
- `sensor.spam_call_ai_google_last_sync`

The safe-contact sensor lists the current Google Contact names in its `contacts`
attribute. Manual trusted numbers are shown as `Manual: +1...`.

Safe callers remain a strict no-AI path. The dashboard records whether the most
recent call was a Safe caller or AI screened. Safe callers that are not answered
on the second ring go to conventional Twilio voicemail; their audio is not sent
to OpenAI.

A complete built-in Home Assistant card is included at:

`spam_call_ai/dashboard-card.yaml`

It uses only built-in Markdown and Entities cards and does not require HACS.


## Collapsible dashboard sections in 0.8.1

The dashboard card can now hide or show these four sections independently:

- Last Call
- Last Call Summary
- Safe Callers
- Google Contacts

Create four Toggle helpers with these entity IDs:

- `input_boolean.spam_call_ai_show_last_call`
- `input_boolean.spam_call_ai_show_summary`
- `input_boolean.spam_call_ai_show_safe_callers`
- `input_boolean.spam_call_ai_show_google_contacts`

The top row of dashboard buttons toggles each section. The implementation uses
only built-in Button and Conditional cards; HACS is not required.
