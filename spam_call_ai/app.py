import asyncio
import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape, quoteattr

import httpx
import websockets
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response
from twilio.request_validator import RequestValidator
from twilio.rest import Client as TwilioClient


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
LOGGER = logging.getLogger("spam_call_ai")

OPTIONS_PATH = Path("/data/options.json")
LAST_CALL_PATH = Path("/data/last_call.json")
PUBLIC_URL_PATH = Path("/data/public_url.txt")
OPENAI_LIVE_URL = "wss://api.openai.com/v1/live/sessions"

app = FastAPI(title="Spam Call AI", version="0.5.0")
active_calls: dict[str, dict[str, Any]] = {}


def load_options() -> dict[str, Any]:
    if not OPTIONS_PATH.exists():
        raise RuntimeError("/data/options.json was not found")
    return json.loads(OPTIONS_PATH.read_text(encoding="utf-8"))


def required_option(options: dict[str, Any], name: str) -> str:
    value = str(options.get(name) or "").strip()
    if not value:
        raise RuntimeError(f"Required option '{name}' is not configured")
    return value


def media_token(options: dict[str, Any]) -> str:
    secret = required_option(options, "twilio_auth_token")
    return hashlib.sha256(("spam-call-ai:" + secret).encode("utf-8")).hexdigest()[:40]


def public_base_url(options: dict[str, Any]) -> str:
    base = str(options.get("public_base_url") or "").strip()

    if not base and PUBLIC_URL_PATH.exists():
        base = PUBLIC_URL_PATH.read_text(encoding="utf-8").strip()

    base = base.rstrip("/")
    if not base:
        raise RuntimeError(
            "No public URL is available yet. Enable auto_tunnel or configure public_base_url."
        )
    if not base.startswith("https://"):
        raise RuntimeError("public_base_url must start with https://")
    return base


def media_ws_url(options: dict[str, Any]) -> str:
    base = public_base_url(options)
    return "wss://" + base.removeprefix("https://") + f"/media/{media_token(options)}"


def safety_identifier(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


CALL_ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "classification": {
            "type": "string",
            "enum": ["legitimate", "telemarketing", "scam", "robocall", "unknown"],
        },
        "spam_likelihood": {
            "type": "number",
            "minimum": 0,
            "maximum": 1,
        },
        "caller_name": {"type": "string"},
        "organization": {"type": "string"},
        "reason": {"type": "string"},
        "callback_number": {"type": "string"},
        "summary": {"type": "string"},
        "recommended_action": {
            "type": "string",
            "enum": ["allow", "block", "review"],
        },
        "signals": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 5,
        },
    },
    "required": [
        "classification",
        "spam_likelihood",
        "caller_name",
        "organization",
        "reason",
        "callback_number",
        "summary",
        "recommended_action",
        "signals",
    ],
    "additionalProperties": False,
}


def extract_response_output_text(payload: dict[str, Any]) -> str:
    for item in payload.get("output", []):
        if item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if content.get("type") == "output_text" and content.get("text"):
                return str(content["text"])
    return ""


async def analyze_call(
    options: dict[str, Any],
    caller: str,
    transcripts: dict[str, str],
) -> dict[str, Any] | None:
    if not bool(options.get("analyze_calls", True)):
        return None

    caller_text = transcripts.get("caller", "").strip()
    assistant_text = transcripts.get("assistant", "").strip()
    if not caller_text and not assistant_text:
        return {
            "classification": "unknown",
            "spam_likelihood": 0.5,
            "caller_name": "",
            "organization": "",
            "reason": "",
            "callback_number": "",
            "summary": "No usable transcript was captured.",
            "recommended_action": "review",
            "signals": [],
        }

    api_key = required_option(options, "openai_api_key")
    model = str(options.get("analysis_model") or "gpt-6-luna").strip()

    transcript = (
        "CALLER TRANSCRIPT:\n"
        + caller_text[-8000:]
        + "\n\nASSISTANT TRANSCRIPT:\n"
        + assistant_text[-8000:]
    )

    system_prompt = (
        "Analyze an inbound phone screening transcript. Use only facts present in "
        "the transcript. Do not guess identity or intent when evidence is weak. "
        "Classify obvious unsolicited sales as telemarketing, deceptive/fraudulent "
        "requests as scam, automated non-human calls as robocall, normal personal "
        "or business calls with a clear legitimate purpose as legitimate, and use "
        "unknown when the evidence is insufficient. Extract a callback number only "
        "if the caller actually stated one. Keep the summary brief. The signals "
        "field must contain only short factual observations from the transcript, "
        "not hidden reasoning or chain-of-thought."
    )

    request_payload = {
        "model": model,
        "store": False,
        "input": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": transcript},
        ],
        "max_output_tokens": 500,
        "text": {
            "format": {
                "type": "json_schema",
                "name": "call_analysis",
                "strict": True,
                "schema": CALL_ANALYSIS_SCHEMA,
            }
        },
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "OpenAI-Safety-Identifier": safety_identifier(caller or "unknown-caller"),
    }

    async with httpx.AsyncClient(timeout=45) as client:
        response = await client.post(
            "https://api.openai.com/v1/responses",
            headers=headers,
            json=request_payload,
        )
        response.raise_for_status()
        payload = response.json()

    output_text = extract_response_output_text(payload)
    if not output_text:
        raise RuntimeError("OpenAI call analysis returned no output text")

    return json.loads(output_text)



def normalize_phone_number(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 10:
        digits = "1" + digits
    if digits:
        return "+" + digits
    return ""


def trusted_caller_numbers(options: dict[str, Any]) -> set[str]:
    raw = str(options.get("trusted_callers") or "")
    numbers: set[str] = set()
    for token in re.split(r"[,;\s]+", raw):
        number = normalize_phone_number(token)
        if number:
            numbers.add(number)
    return numbers


def is_trusted_caller(options: dict[str, Any], caller: str) -> bool:
    return normalize_phone_number(caller) in trusted_caller_numbers(options)


def ai_stream_xml(
    options: dict[str, Any],
    caller: str,
    call_sid: str,
    to_number: str,
    include_greeting: bool = True,
) -> str:
    greeting = str(options.get("greeting") or "").strip()
    stream_url = media_ws_url(options)

    parameter_xml = (
        f'<Parameter name="caller" value={quoteattr(caller)} />'
        f'<Parameter name="callSid" value={quoteattr(call_sid)} />'
        f'<Parameter name="to" value={quoteattr(to_number)} />'
    )
    say_xml = (
        f"<Say>{escape(greeting)}</Say>"
        if include_greeting and greeting
        else ""
    )
    return (
        f"{say_xml}"
        "<Connect>"
        f"<Stream url={quoteattr(stream_url)}>{parameter_xml}</Stream>"
        "</Connect>"
    )


def transfer_is_configured(options: dict[str, Any]) -> bool:
    return bool(options.get("live_transfer_enabled", False)) and bool(
        str(options.get("forward_to_number") or "").strip()
    )


def transfer_backend_prompt() -> str:
    return """
You are the conservative transfer-decision backend for an inbound call screener.

The voice assistant may delegate when a caller asks to speak with the phone owner.
Your job is to decide whether the current caller is clearly legitimate enough to
attempt a transfer.

Only call transfer_to_owner when ALL of these are true:
- The caller explicitly asked to speak with, reach, or be connected to a person
  at this number, including asking for someone by name.
- The caller gave a coherent specific reason for the call.
- The call appears personal or legitimate business-related.
- There are no meaningful scam, telemarketing, collection, political fundraising,
  survey, warranty, tech-support, financial-pressure, gift-card, crypto, login,
  verification-code, or account-takeover signals.
- You are at least 0.92 confident it is appropriate to transfer.

If any condition is uncertain, do NOT call the tool. Let the voice assistant take
a message instead.

Never transfer a caller merely because they claim urgency, authority, or a known
company. Never treat the caller's self-asserted identity as verified.
""".strip()


def prompt_text(transfer_enabled: bool = False) -> str:
    transfer_text = """
Live transfer is available.
- When a caller explicitly asks to speak with, reach, or be connected to a person
  at this number, including asking for someone by name, you MUST delegate the
  transfer decision to the backend after you have their name/organization when
  available and a specific reason for the call.
- Do not require them to use the words "owner" or "transfer."
- Tell them briefly that you will check whether you can connect them, then delegate.
- Do not promise a transfer before the backend approves it.
- If the backend does not approve a transfer, continue screening or take a message.
""" if transfer_enabled else """
Live transfer is not enabled. Take a concise message for legitimate callers and
never promise an immediate transfer.
"""

    return f"""
You are an automated telephone screening assistant for an inbound phone number.

The caller has already heard: "Hi, you've reached the automated call assistant.
Who's calling, and what are you calling about?"

Your role is to protect the phone owner from spam, scams, telemarketing, and
unwanted solicitation while remaining courteous to legitimate callers.

Speaking style:
- Keep replies short, natural, and conversational, usually one or two sentences.
- Be calm, friendly, patient, and inquisitive.
- Do not claim to be human. If asked, say you are an automated AI call assistant.
- Never claim to be law enforcement, a bank, government agency, lawyer, medical
  professional, or another real person.
- Do not threaten, insult, harass, or use sexual content.

Privacy and security:
- Never reveal, infer, confirm, or invent private information about the phone owner.
- Never provide names, addresses, dates of birth, email addresses, passwords,
  PINs, verification codes, account numbers, Social Security numbers, card or
  banking information, device details, travel plans, family details, or whether
  the owner is home or away.
- Never open links, visit websites, download software, install apps, send money,
  buy gift cards, transfer cryptocurrency, or agree to a purchase.
- Never help a caller complete a login, payment, identity-verification process,
  remote-access session, or account takeover.

Call screening:
- Ask the caller's name, organization, and reason for calling when useful.
- If the caller seems legitimate, politely gather a brief message and callback
  number if they voluntarily provide it. Say the message can be passed along.
{transfer_text}

Suspected spam, scam, robocall, or unsolicited sales:
- Keep the conversation going without giving useful personal information.
- Ask reasonable follow-up questions and ask the caller to explain vague claims.
- Occasionally ask them to repeat or clarify details.
- Do not disclose that the purpose is to occupy their time.
- If asked for sensitive information, decline or redirect with a question.
- Do not fabricate sensitive data just to keep the caller talking.
- End the conversation if it becomes threatening, abusive, or unsafe.

Useful questions include:
- "What company did you say you're calling from?"
- "What is this regarding?"
- "How did you get this number?"
- "Can you explain that again?"
- "What department are you with?"
- "What would happen if I don't do that?"
- "Can you give me the reference number again?"
""".strip()



async def update_twilio_call_for_transfer(
    options: dict[str, Any],
    call_sid: str,
) -> None:
    account_sid = required_option(options, "twilio_account_sid")
    auth_token = required_option(options, "twilio_auth_token")
    transfer_url = public_base_url(options) + "/transfer"

    def _update() -> None:
        client = TwilioClient(account_sid, auth_token)
        client.calls(call_sid).update(url=transfer_url, method="POST")

    await asyncio.to_thread(_update)


async def handle_transfer_request(
    options: dict[str, Any],
    call_sid: str,
    caller: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    enabled = bool(options.get("live_transfer_enabled", False))
    destination = str(options.get("forward_to_number") or "").strip()
    min_confidence = float(options.get("transfer_min_confidence", 0.92))

    classification = str(arguments.get("classification") or "").lower()
    confidence = float(arguments.get("confidence") or 0)
    requested = bool(arguments.get("caller_requested_transfer", False))
    reason = str(arguments.get("reason") or "").strip()
    caller_name = str(arguments.get("caller_name") or "").strip()
    organization = str(arguments.get("organization") or "").strip()

    audit = {
        "caller": caller,
        "call_sid": call_sid,
        "classification": classification,
        "confidence": confidence,
        "caller_requested_transfer": requested,
        "reason": reason,
        "caller_name": caller_name,
        "organization": organization,
    }

    await fire_home_assistant_event("spam_call_ai_transfer_requested", audit)

    denial = None
    if not enabled:
        denial = "Live transfer is disabled."
    elif not destination:
        denial = "No transfer destination is configured."
    elif not call_sid:
        denial = "The active Twilio call SID is unavailable."
    elif classification != "legitimate":
        denial = "The backend did not classify the caller as legitimate."
    elif confidence < min_confidence:
        denial = f"Transfer confidence {confidence:.2f} is below the required {min_confidence:.2f}."
    elif not requested:
        denial = "The caller did not explicitly request a transfer."
    elif not reason:
        denial = "No specific reason for the call was provided."

    if denial:
        LOGGER.info("Transfer denied: %s", denial)
        await fire_home_assistant_event(
            "spam_call_ai_transfer_denied",
            {**audit, "denial_reason": denial},
        )
        return {"status": "denied", "reason": denial}

    try:
        await update_twilio_call_for_transfer(options, call_sid)
    except Exception as exc:
        LOGGER.exception("Twilio transfer failed: %s", exc)
        await fire_home_assistant_event(
            "spam_call_ai_transfer_failed",
            {**audit, "error": str(exc)},
        )
        return {"status": "failed", "reason": "Twilio could not start the transfer."}

    LOGGER.info(
        "Transfer started: caller=%s confidence=%.2f reason=%s",
        caller,
        confidence,
        reason,
    )
    await fire_home_assistant_event(
        "spam_call_ai_transfer_started",
        audit,
    )
    return {"status": "transfer_started"}


async def fire_home_assistant_event(event_type: str, data: dict[str, Any]) -> None:
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        LOGGER.warning("SUPERVISOR_TOKEN is unavailable; skipping Home Assistant event")
        return

    url = f"http://supervisor/core/api/events/{event_type}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(url, headers=headers, json=data)
            response.raise_for_status()
    except Exception as exc:
        LOGGER.warning("Could not fire Home Assistant event %s: %s", event_type, exc)


def save_last_call(data: dict[str, Any]) -> None:
    try:
        LAST_CALL_PATH.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as exc:
        LOGGER.warning("Could not save last call: %s", exc)


def validate_twilio_http_request(
    request: Request,
    form_data: dict[str, Any],
    options: dict[str, Any],
) -> bool:
    if not bool(options.get("validate_twilio_signature", True)):
        return True

    signature = request.headers.get("x-twilio-signature", "")
    if not signature:
        return False

    validator = RequestValidator(required_option(options, "twilio_auth_token"))
    validation_url = public_base_url(options) + request.url.path
    return validator.validate(validation_url, form_data, signature)


@app.get("/health")
async def health() -> JSONResponse:
    try:
        options = load_options()
        required_option(options, "openai_api_key")
        required_option(options, "twilio_account_sid")
        required_option(options, "twilio_auth_token")
        base = public_base_url(options)
        configured = True
    except Exception:
        base = None
        configured = False

    return JSONResponse(
        {
            "ok": True,
            "configured": configured,
            "public_base_url": base,
            "active_calls": len(active_calls),
        }
    )


@app.get("/status")
async def status() -> JSONResponse:
    options = load_options()
    try:
        base = public_base_url(options)
    except Exception:
        base = None

    last_call = None
    if LAST_CALL_PATH.exists():
        try:
            last_call = json.loads(LAST_CALL_PATH.read_text(encoding="utf-8"))
        except Exception:
            last_call = None

    return JSONResponse(
        {
            "public_base_url": base,
            "twilio_webhook_url": f"{base}/twiml" if base else None,
            "active_calls": len(active_calls),
            "last_call": last_call,
        }
    )



@app.post("/transfer")
async def transfer_twiml(request: Request) -> Response:
    options = load_options()
    form = await request.form()
    form_data = dict(form)

    if not validate_twilio_http_request(request, form_data, options):
        raise HTTPException(status_code=403, detail="Invalid Twilio signature")

    destination = str(options.get("forward_to_number") or "").strip()
    if not bool(options.get("live_transfer_enabled", False)) or not destination:
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            "<Response>"
            "<Say>I'm sorry, live transfer is not available right now.</Say>"
            "<Hangup/>"
            "</Response>"
        )
        return Response(content=xml, media_type="application/xml")

    timeout = int(options.get("transfer_timeout_seconds", 25))
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        "<Say>Please hold while I try to connect your call.</Say>"
        f'<Dial answerOnBridge="true" timeout="{timeout}">'
        f"<Number>{escape(destination)}</Number>"
        "</Dial>"
        "<Say>I couldn't reach them. Please call back later.</Say>"
        "<Hangup/>"
        "</Response>"
    )
    return Response(content=xml, media_type="application/xml")


@app.post("/twiml")
async def twiml(request: Request) -> Response:
    options = load_options()
    form = await request.form()
    form_data = dict(form)

    if not validate_twilio_http_request(request, form_data, options):
        raise HTTPException(status_code=403, detail="Invalid Twilio signature")

    caller = str(form_data.get("From") or "unknown")
    call_sid = str(form_data.get("CallSid") or "")
    account_sid = str(form_data.get("AccountSid") or "")
    to_number = str(form_data.get("To") or "")

    expected_account = required_option(options, "twilio_account_sid")
    if account_sid and account_sid != expected_account:
        raise HTTPException(status_code=403, detail="Unexpected Twilio account")

    caller_norm = normalize_phone_number(caller)
    to_norm = normalize_phone_number(to_number)

    # Loop guard: trusted re-ring uses the Twilio number as outbound caller ID.
    # If that second ring is itself conditionally forwarded back to Twilio,
    # From and To are the Twilio number. End that child call instead of re-ringing.
    if caller_norm and to_norm and caller_norm == to_norm:
        LOGGER.info("Stopped trusted-caller re-ring loop for call_sid=%s", call_sid)
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            "<Response><Hangup/></Response>"
        )
        return Response(content=xml, media_type="application/xml")

    trusted_rering = bool(options.get("trusted_rering_enabled", True))
    destination = str(options.get("forward_to_number") or "").strip()

    if trusted_rering and destination and is_trusted_caller(options, caller):
        timeout = int(options.get("trusted_rering_timeout_seconds", 15))
        LOGGER.info("Trusted caller recognized; re-ringing the cell: caller=%s", caller)
        await fire_home_assistant_event(
            "spam_call_ai_trusted_caller_rering",
            {
                "caller": caller,
                "call_sid": call_sid,
                "destination_configured": True,
            },
        )

        # Use the Twilio number as caller ID on the second ring. That makes a
        # conditionally-forwarded unanswered second ring easy to identify and
        # terminate above, preventing an infinite forwarding loop.
        dial_xml = (
            f'<Dial answerOnBridge="true" timeout="{timeout}" '
            f'callerId={quoteattr(to_number)}>'
            f"<Number>{escape(destination)}</Number>"
            "</Dial>"
        )

        # If the second ring is not answered, continue the original family call
        # into the AI so it can take a message.
        fallback_xml = ai_stream_xml(
            options,
            caller,
            call_sid,
            to_number,
            include_greeting=True,
        )
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            "<Response>"
            f"{dial_xml}"
            f"{fallback_xml}"
            "<Hangup/>"
            "</Response>"
        )
        return Response(content=xml, media_type="application/xml")

    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        f"{ai_stream_xml(options, caller, call_sid, to_number, include_greeting=True)}"
        "<Hangup/>"
        "</Response>"
    )

    return Response(content=xml, media_type="application/xml")


async def wait_for_openai_session_started(openai_ws) -> None:
    while True:
        raw = await asyncio.wait_for(openai_ws.recv(), timeout=20)
        event = json.loads(raw)
        event_type = event.get("type")

        if event_type == "session.started":
            return
        if event_type == "error":
            raise RuntimeError(f"OpenAI session error: {event}")


async def twilio_to_openai(twilio_ws: WebSocket, openai_ws) -> None:
    while True:
        try:
            message = await twilio_ws.receive_text()
        except WebSocketDisconnect:
            return

        data = json.loads(message)
        event = data.get("event")

        if event == "media":
            payload = data.get("media", {}).get("payload")
            if payload:
                await openai_ws.send(
                    json.dumps(
                        {
                            "type": "session.input_audio.append",
                            "audio": payload,
                        }
                    )
                )
        elif event == "stop":
            return


async def openai_to_twilio(
    openai_ws,
    twilio_ws: WebSocket,
    stream_sid: str,
    transcripts: dict[str, str],
    options: dict[str, Any],
    call_sid: str,
    caller: str,
) -> None:
    async for raw in openai_ws:
        event = json.loads(raw)
        event_type = event.get("type")

        if event_type == "session.output_audio.delta":
            delta = event.get("delta")
            if delta:
                await twilio_ws.send_json(
                    {
                        "event": "media",
                        "streamSid": stream_sid,
                        "media": {"payload": delta},
                    }
                )

        elif event_type == "session.input_transcript.delta":
            transcripts["caller"] += str(event.get("delta") or "")

        elif event_type == "session.output_transcript.delta":
            transcripts["assistant"] += str(event.get("delta") or "")

        elif event_type == "session.delegation.created":
            delegation = event.get("delegation") or {}
            LOGGER.info(
                "Live transfer check delegated: id=%s target=%s",
                delegation.get("id"),
                delegation.get("target"),
            )

        elif event_type == "response.event":
            nested = event.get("event") or {}
            if nested.get("type") == "response.output_item.done":
                item = nested.get("item") or {}
                if item.get("type") == "function_call" and item.get("name") == "transfer_to_owner":
                    LOGGER.info("Transfer decision tool requested by backend")
                    call_id = str(item.get("call_id") or "")
                    try:
                        arguments = json.loads(str(item.get("arguments") or "{}"))
                    except json.JSONDecodeError:
                        arguments = {}

                    tool_result = await handle_transfer_request(
                        options,
                        call_sid,
                        caller,
                        arguments,
                    )

                    if call_id:
                        try:
                            await openai_ws.send(
                                json.dumps(
                                    {
                                        "type": "response.item.create",
                                        "item": {
                                            "type": "function_call_output",
                                            "call_id": call_id,
                                            "output": json.dumps(tool_result),
                                        },
                                    }
                                )
                            )
                            await openai_ws.send(json.dumps({"type": "response.create"}))
                        except Exception as exc:
                            LOGGER.warning("Could not return transfer tool result: %s", exc)

        elif event_type == "error":
            LOGGER.error("OpenAI error: %s", event)

        elif event_type == "session.closed":
            return


@app.websocket("/media/{token}")
async def media(websocket: WebSocket, token: str) -> None:
    options = load_options()
    if token != media_token(options):
        await websocket.close(code=1008)
        return

    await websocket.accept()

    stream_sid = ""
    caller = "unknown"
    call_sid = ""
    start_time = time.monotonic()
    transcripts = {"caller": "", "assistant": ""}

    try:
        first = json.loads(await asyncio.wait_for(websocket.receive_text(), timeout=10))
        if first.get("event") == "connected":
            start = json.loads(await asyncio.wait_for(websocket.receive_text(), timeout=10))
        else:
            start = first

        if start.get("event") != "start":
            await websocket.close(code=1008)
            return

        start_data = start.get("start", {})
        stream_sid = str(start.get("streamSid") or start_data.get("streamSid") or "")
        account_sid = str(start_data.get("accountSid") or "")
        params = start_data.get("customParameters") or {}
        caller = str(params.get("caller") or "unknown")
        call_sid = str(params.get("callSid") or start_data.get("callSid") or "")

        expected_account = required_option(options, "twilio_account_sid")
        if account_sid and account_sid != expected_account:
            LOGGER.warning("Rejected media stream from unexpected Twilio account")
            await websocket.close(code=1008)
            return

        call_key = call_sid or stream_sid or f"call-{time.time_ns()}"
        active_calls[call_key] = {
            "caller": caller,
            "call_sid": call_sid,
            "stream_sid": stream_sid,
            "started": time.time(),
        }

        await fire_home_assistant_event(
            "spam_call_ai_call_started",
            {
                "caller": caller,
                "call_sid": call_sid,
                "stream_sid": stream_sid,
            },
        )

        api_key = required_option(options, "openai_api_key")
        voice = str(options.get("voice") or "marin").strip()
        max_seconds = int(options.get("max_call_minutes", 10)) * 60
        transfer_enabled = transfer_is_configured(options)
        analysis_model = str(options.get("analysis_model") or "gpt-6-luna").strip()

        headers = {
            "Authorization": f"Bearer {api_key}",
            "OpenAI-Safety-Identifier": safety_identifier(caller or call_sid or stream_sid),
        }

        async with websockets.connect(
            OPENAI_LIVE_URL,
            additional_headers=headers,
            open_timeout=20,
            close_timeout=10,
            ping_interval=20,
            ping_timeout=20,
            max_size=None,
        ) as openai_ws:
            await openai_ws.send(
                json.dumps(
                    {
                        "type": "session.start",
                        "session": {
                            "model": "gpt-live-1",
                            "instructions": prompt_text(transfer_enabled),
                            **(
                                {
                                    "delegation": {
                                        "type": "responses",
                                        "responses": {
                                            "model": analysis_model,
                                            "instructions": transfer_backend_prompt(),
                                            "tools": [
                                                {
                                                    "type": "function",
                                                    "name": "transfer_to_owner",
                                                    "description": (
                                                        "Attempt to transfer a clearly legitimate caller "
                                                        "who explicitly asked to speak with the phone owner."
                                                    ),
                                                    "parameters": {
                                                        "type": "object",
                                                        "properties": {
                                                            "classification": {
                                                                "type": "string",
                                                                "enum": [
                                                                    "legitimate",
                                                                    "telemarketing",
                                                                    "scam",
                                                                    "robocall",
                                                                    "unknown",
                                                                ],
                                                            },
                                                            "confidence": {
                                                                "type": "number",
                                                                "minimum": 0,
                                                                "maximum": 1,
                                                            },
                                                            "caller_requested_transfer": {
                                                                "type": "boolean"
                                                            },
                                                            "caller_name": {"type": "string"},
                                                            "organization": {"type": "string"},
                                                            "reason": {"type": "string"},
                                                        },
                                                        "required": [
                                                            "classification",
                                                            "confidence",
                                                            "caller_requested_transfer",
                                                            "caller_name",
                                                            "organization",
                                                            "reason",
                                                        ],
                                                        "additionalProperties": False,
                                                    },
                                                }
                                            ],
                                            "tool_choice": "required",
                                            "parallel_tool_calls": False,
                                            "max_output_tokens": 200,
                                        },
                                    }
                                }
                                if transfer_enabled
                                else {}
                            ),
                            "audio": {
                                "format": {
                                    "type": "audio/pcmu",
                                    "rate": 8000,
                                },
                                "output": {
                                    "voice": voice,
                                },
                            },
                        },
                    }
                )
            )

            await wait_for_openai_session_started(openai_ws)

            inbound = asyncio.create_task(
                twilio_to_openai(websocket, openai_ws),
                name="twilio_to_openai",
            )
            outbound = asyncio.create_task(
                openai_to_twilio(
                    openai_ws,
                    websocket,
                    stream_sid,
                    transcripts,
                    options,
                    call_sid,
                    caller,
                ),
                name="openai_to_twilio",
            )

            done, pending = await asyncio.wait(
                {inbound, outbound},
                timeout=max_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )

            for task in pending:
                task.cancel()

            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

            try:
                await openai_ws.send(json.dumps({"type": "session.close"}))
            except Exception:
                pass

            for task in done:
                exc = task.exception()
                if exc:
                    raise exc

    except WebSocketDisconnect:
        pass
    except asyncio.TimeoutError:
        LOGGER.info("Call reached a timeout")
    except Exception as exc:
        LOGGER.exception("Call bridge error: %s", exc)
    finally:
        duration = max(0, round(time.monotonic() - start_time, 1))
        call_key = call_sid or stream_sid
        if call_key:
            active_calls.pop(call_key, None)

        result = {
            "caller": caller,
            "call_sid": call_sid,
            "stream_sid": stream_sid,
            "duration_seconds": duration,
            "caller_transcript": transcripts["caller"][-4000:],
            "assistant_transcript": transcripts["assistant"][-4000:],
            "ended_at_unix": time.time(),
        }

        try:
            analysis = await analyze_call(options, caller, transcripts)
            if analysis is not None:
                result["analysis"] = analysis
                LOGGER.info(
                    "Call analysis: classification=%s spam_likelihood=%s action=%s summary=%s",
                    analysis.get("classification"),
                    analysis.get("spam_likelihood"),
                    analysis.get("recommended_action"),
                    analysis.get("summary"),
                )
                await fire_home_assistant_event(
                    "spam_call_ai_call_analyzed",
                    {
                        "caller": caller,
                        "call_sid": call_sid,
                        "duration_seconds": duration,
                        **analysis,
                    },
                )
        except Exception as exc:
            LOGGER.warning("Call analysis failed: %s", exc)
            result["analysis_error"] = str(exc)

        save_last_call(result)
        await fire_home_assistant_event("spam_call_ai_call_ended", result)

        try:
            await websocket.close()
        except Exception:
            pass
