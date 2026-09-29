import asyncio
import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape, quoteattr

import httpx
import websockets
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response
from twilio.request_validator import RequestValidator


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
LOGGER = logging.getLogger("spam_call_ai")

OPTIONS_PATH = Path("/data/options.json")
LAST_CALL_PATH = Path("/data/last_call.json")
PUBLIC_URL_PATH = Path("/data/public_url.txt")
OPENAI_LIVE_URL = "wss://api.openai.com/v1/live/sessions"

app = FastAPI(title="Spam Call AI", version="0.1.1")
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


def prompt_text() -> str:
    return """
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
- This version cannot transfer calls, so never promise an immediate transfer.

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

    greeting = str(options.get("greeting") or "").strip()
    stream_url = media_ws_url(options)

    parameter_xml = (
        f'<Parameter name="caller" value={quoteattr(caller)} />'
        f'<Parameter name="callSid" value={quoteattr(call_sid)} />'
        f'<Parameter name="to" value={quoteattr(to_number)} />'
    )

    say_xml = f"<Say>{escape(greeting)}</Say>" if greeting else ""
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        f"{say_xml}"
        "<Connect>"
        f"<Stream url={quoteattr(stream_url)}>{parameter_xml}</Stream>"
        "</Connect>"
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
                            "instructions": prompt_text(),
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
                openai_to_twilio(openai_ws, websocket, stream_sid, transcripts),
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
        save_last_call(result)
        await fire_home_assistant_event("spam_call_ai_call_ended", result)

        try:
            await websocket.close()
        except Exception:
            pass
