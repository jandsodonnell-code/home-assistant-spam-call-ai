import asyncio
import logging
import os
import time
from pathlib import Path
from datetime import datetime, timezone
from xml.sax.saxutils import escape, quoteattr

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import Response

import app as base

LOGGER = logging.getLogger("spam_call_ai.safe")
app = base.app
SAFE_MESSAGE_PATH = Path("/data/last_safe_message.json")
SAFE_CALL_PATH = Path("/data/last_safe_call.json")
SCREEN_PROMPT_DIR = Path("/data/spam_call_ai_prompts")
SCREEN_PROMPT_DIR.mkdir(parents=True, exist_ok=True)

RETRY_PROMPT = "Hello? Are you there? Who's calling, and what are you calling about?"


def _prompt_token(options: dict) -> str:
    return base.media_token(options)


def _prompt_url(options: dict, kind: str) -> str:
    token = _prompt_token(options)
    return f"{base.public_base_url(options)}/screen/prompt/{token}/{kind}.wav"


async def _screen_prompt_wav(options: dict, kind: str) -> bytes:
    if kind == "opening":
        text = str(options.get("greeting") or "").strip()
        if not text:
            text = (
                "Hi, you've reached the automated call assistant. "
                "Who's calling, and what are you calling about?"
            )
    elif kind == "retry":
        text = RETRY_PROMPT
    else:
        raise HTTPException(status_code=404, detail="Unknown prompt")

    voice = str(options.get("voice") or "marin").strip()
    cache_key = base.hashlib.sha256(
        f"gpt-4o-mini-tts|{voice}|{text}".encode("utf-8")
    ).hexdigest()[:20]
    path = SCREEN_PROMPT_DIR / f"{kind}-{cache_key}.wav"

    if path.exists() and path.stat().st_size > 44:
        return path.read_bytes()

    api_key = base.required_option(options, "openai_api_key")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": "gpt-4o-mini-tts",
        "voice": voice,
        "input": text,
        "instructions": (
            "Speak as a calm, friendly automated call screening assistant. "
            "Use a natural phone cadence and clear diction."
        ),
        "response_format": "wav",
    }

    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(
            "https://api.openai.com/v1/audio/speech",
            headers=headers,
            json=payload,
        )
        response.raise_for_status()
        wav = response.content

    path.write_bytes(wav)
    LOGGER.info("Generated cached %s screening prompt with voice=%s", kind, voice)
    return wav


def _validate_twilio_action(
    request: Request,
    form_data: dict,
    options: dict,
) -> None:
    if not base.validate_twilio_http_request(request, form_data, options):
        raise HTTPException(status_code=403, detail="Invalid Twilio signature")

    expected_account = base.required_option(options, "twilio_account_sid")
    account_sid = str(form_data.get("AccountSid") or "")
    if account_sid and account_sid != expected_account:
        raise HTTPException(status_code=403, detail="Unexpected Twilio account")


def _live_screen_twiml(
    options: dict,
    caller: str,
    call_sid: str,
    to_number: str,
    initial_speech: str,
) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        f"{base.ai_stream_xml(options, caller, call_sid, to_number, include_greeting=False, initial_speech=initial_speech)}"
        "<Hangup/>"
        "</Response>"
    )


def _first_screen_gather_twiml(options: dict) -> str:
    action_url = base.public_base_url(options) + "/screen/first-result"
    prompt_url = _prompt_url(options, "opening")
    timeout = int(options.get("initial_response_timeout_seconds", 8))
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        f'<Gather input="speech" timeout="{timeout}" speechTimeout="auto" '
        f'actionOnEmptyResult="true" action={quoteattr(action_url)} method="POST">'
        f"<Play>{escape(prompt_url)}</Play>"
        "</Gather>"
        "</Response>"
    )


def _second_screen_gather_twiml(options: dict) -> str:
    action_url = base.public_base_url(options) + "/screen/second-result"
    prompt_url = _prompt_url(options, "retry")
    timeout = int(options.get("second_response_timeout_seconds", 7))
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        f'<Gather input="speech" timeout="{timeout}" speechTimeout="auto" '
        f'action={quoteattr(action_url)} method="POST">'
        f"<Play>{escape(prompt_url)}</Play>"
        "</Gather>"
        "<Hangup/>"
        "</Response>"
    )


@app.get("/screen/prompt/{token}/{kind}.wav")
async def screening_prompt_audio(token: str, kind: str) -> Response:
    options = base.load_options()
    if token != _prompt_token(options):
        raise HTTPException(status_code=404, detail="Not found")
    wav = await _screen_prompt_wav(options, kind)
    return Response(
        content=wav,
        media_type="audio/wav",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.post("/screen/first-result")
async def screen_first_result(request: Request) -> Response:
    options = base.load_options()
    form = await request.form()
    form_data = dict(form)
    _validate_twilio_action(request, form_data, options)

    caller = str(form_data.get("From") or "unknown")
    call_sid = str(form_data.get("CallSid") or "")
    to_number = str(form_data.get("To") or "")
    speech = str(form_data.get("SpeechResult") or "").strip()

    if speech:
        LOGGER.info("Initial screening speech captured; starting GPT-Live")
        xml = _live_screen_twiml(
            options, caller, call_sid, to_number, speech
        )
    else:
        LOGGER.info(
            "No speech after %ss opening window; playing one retry prompt",
            int(options.get("initial_response_timeout_seconds", 8)),
        )
        xml = _second_screen_gather_twiml(options)

    return Response(content=xml, media_type="application/xml")


@app.post("/screen/second-result")
async def screen_second_result(request: Request) -> Response:
    options = base.load_options()
    form = await request.form()
    form_data = dict(form)
    _validate_twilio_action(request, form_data, options)

    caller = str(form_data.get("From") or "unknown")
    call_sid = str(form_data.get("CallSid") or "")
    to_number = str(form_data.get("To") or "")
    speech = str(form_data.get("SpeechResult") or "").strip()

    if speech:
        LOGGER.info("Speech captured after retry prompt; starting GPT-Live")
        xml = _live_screen_twiml(
            options, caller, call_sid, to_number, speech
        )
    else:
        LOGGER.info(
            "No speech after retry prompt and %ss wait; ending call",
            int(options.get("second_response_timeout_seconds", 7)),
        )
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            "<Response><Hangup/></Response>"
        )

    return Response(content=xml, media_type="application/xml")


@app.on_event("startup")
async def warm_screening_prompts() -> None:
    async def _warm() -> None:
        try:
            options = base.load_options()
            await _screen_prompt_wav(options, "opening")
            await _screen_prompt_wav(options, "retry")
            LOGGER.info("Screening voice prompts ready")
        except Exception as exc:
            LOGGER.warning("Could not pre-generate screening prompts: %s", exc)

    asyncio.create_task(_warm(), name="warm_screening_prompts")


# Replace only the inbound Twilio route. Everything else remains in app.py.
app.router.routes[:] = [
    route for route in app.router.routes
    if getattr(route, "path", None) != "/twiml"
]


def _safe_contact_names(options: dict) -> list[str]:
    names: list[str] = []
    data = base.load_json_file(base.GOOGLE_CONTACTS_PATH)

    if bool(options.get("google_contacts_enabled", False)):
        for contact in data.get("contacts") or []:
            name = str(contact.get("name") or "").strip()
            if name and name not in names:
                names.append(name)

    manual = str(options.get("trusted_callers") or "").strip()
    if manual:
        for token in base.re.split(r"[,;\s]+", manual):
            number = base.normalize_phone_number(token)
            if number:
                label = f"Manual: {number}"
                if label not in names:
                    names.append(label)

    return sorted(names, key=str.casefold)


def _iso_utc(value) -> str:
    try:
        stamp = float(value or 0)
    except (TypeError, ValueError):
        stamp = 0
    if stamp <= 0:
        return "Never"
    return datetime.fromtimestamp(stamp, tz=timezone.utc).isoformat()


def _state_text(value, fallback: str = "Unknown") -> str:
    text = str(value or "").strip()
    return text if text else fallback


def _last_dashboard_activity() -> dict:
    ai_call = base.load_json_file(base.LAST_CALL_PATH)
    safe_call = base.load_json_file(SAFE_CALL_PATH)

    try:
        ai_time = float(ai_call.get("ended_at_unix") or 0)
    except (TypeError, ValueError):
        ai_time = 0
    try:
        safe_time = float(safe_call.get("updated_at_unix") or 0)
    except (TypeError, ValueError):
        safe_time = 0

    if safe_time >= ai_time and safe_time > 0:
        status = str(safe_call.get("status") or "safe").replace("_", " ").title()
        summary = str(
            safe_call.get("summary")
            or "Safe caller bypassed AI."
        ).strip()
        return {
            "caller": str(safe_call.get("caller") or "Unknown"),
            "caller_name": str(safe_call.get("trusted_name") or "Safe caller"),
            "call_type": "Safe caller",
            "classification": "Safe",
            "spam_likelihood_percent": 0,
            "summary": summary,
            "callback_number": "",
            "duration_seconds": safe_call.get("duration_seconds") or 0,
            "action": status,
            "time_unix": safe_time,
            "ai_used": False,
            "status": status,
        }

    if ai_time > 0:
        analysis = ai_call.get("analysis") or {}
        try:
            spam_percent = round(float(analysis.get("spam_likelihood") or 0) * 100)
        except (TypeError, ValueError):
            spam_percent = 0
        classification = _state_text(
            analysis.get("classification"),
            "Unknown",
        ).title()
        return {
            "caller": str(ai_call.get("caller") or "Unknown"),
            "caller_name": _state_text(analysis.get("caller_name"), "Unknown"),
            "call_type": "AI screened",
            "classification": classification,
            "spam_likelihood_percent": spam_percent,
            "summary": _state_text(
                analysis.get("summary"),
                "AI-screened call completed.",
            ),
            "callback_number": str(analysis.get("callback_number") or ""),
            "duration_seconds": ai_call.get("duration_seconds") or 0,
            "action": _state_text(
                analysis.get("recommended_action"),
                "Review",
            ).title(),
            "time_unix": ai_time,
            "ai_used": True,
            "status": "Completed",
        }

    return {
        "caller": "No calls yet",
        "caller_name": "No calls yet",
        "call_type": "No calls yet",
        "classification": "No calls yet",
        "spam_likelihood_percent": "unknown",
        "summary": "No calls yet",
        "callback_number": "",
        "duration_seconds": "unknown",
        "action": "No calls yet",
        "time_unix": 0,
        "ai_used": False,
        "status": "No calls yet",
    }


async def _write_ha_states(states: list[tuple[str, object, dict]]) -> None:
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        LOGGER.error("Dashboard entities: SUPERVISOR_TOKEN is unavailable")
        return

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=10) as client:
        for entity_id, state, attributes in states:
            response = await client.post(
                f"http://supervisor/core/api/states/{entity_id}",
                headers=headers,
                json={
                    "state": str(state),
                    "attributes": attributes,
                },
            )
            response.raise_for_status()


async def _publish_dashboard_entities() -> None:
    activity = _last_dashboard_activity()
    google = base.google_contacts_status()
    last_sync = _iso_utc(google.get("last_sync_unix"))
    blocked_entries = base.blocked_caller_entries()
    blocked_numbers = sorted(base.blocked_caller_numbers())

    if google.get("last_error"):
        google_status = "Error"
    elif google.get("last_sync_unix"):
        google_status = "Synced"
    elif google.get("authorized"):
        google_status = "Authorized"
    else:
        google_status = "Not authorized"

    summary_state = str(activity["summary"])[:250]
    callback_state = str(activity["callback_number"] or "None")

    common_last_call_attrs = {
        "friendly_name": "Spam Call AI Last Call",
        "icon": "mdi:phone-log",
        "caller": activity["caller"],
        "caller_name": activity["caller_name"],
        "call_type": activity["call_type"],
        "classification": activity["classification"],
        "spam_likelihood_percent": activity["spam_likelihood_percent"],
        "summary": activity["summary"],
        "callback_number": activity["callback_number"],
        "duration_seconds": activity["duration_seconds"],
        "action": activity["action"],
        "status": activity["status"],
        "time": _iso_utc(activity["time_unix"]),
        "ai_used": activity["ai_used"],
    }

    states = [
        (
            "binary_sensor.spam_call_ai_online",
            "on",
            {
                "friendly_name": "Spam Call AI Online",
                "icon": "mdi:phone-check",
            },
        ),
        (
            "sensor.spam_call_ai_active_calls",
            len(base.active_calls),
            {
                "friendly_name": "Spam Call AI Active Calls",
                "icon": "mdi:phone-in-talk",
            },
        ),
        (
            "sensor.spam_call_ai_last_call",
            activity["classification"],
            common_last_call_attrs,
        ),
        (
            "sensor.spam_call_ai_last_caller",
            activity["caller"],
            {
                "friendly_name": "Spam Call AI Last Caller",
                "icon": "mdi:phone",
            },
        ),
        (
            "sensor.spam_call_ai_last_caller_name",
            activity["caller_name"],
            {
                "friendly_name": "Spam Call AI Last Caller Name",
                "icon": "mdi:account",
            },
        ),
        (
            "sensor.spam_call_ai_last_call_type",
            activity["call_type"],
            {
                "friendly_name": "Spam Call AI Last Call Type",
                "icon": "mdi:shield-phone",
            },
        ),
        (
            "sensor.spam_call_ai_last_classification",
            activity["classification"],
            {
                "friendly_name": "Spam Call AI Last Classification",
                "icon": "mdi:shield-search",
            },
        ),
        (
            "sensor.spam_call_ai_last_spam_likelihood",
            activity["spam_likelihood_percent"],
            {
                "friendly_name": "Spam Call AI Last Spam Likelihood",
                "icon": "mdi:percent",
                "unit_of_measurement": "%",
            },
        ),
        (
            "sensor.spam_call_ai_last_action",
            activity["action"],
            {
                "friendly_name": "Spam Call AI Last Action",
                "icon": "mdi:phone-check-outline",
            },
        ),
        (
            "sensor.spam_call_ai_last_duration",
            activity["duration_seconds"],
            {
                "friendly_name": "Spam Call AI Last Duration",
                "icon": "mdi:timer-outline",
                "unit_of_measurement": "s",
            },
        ),
        (
            "sensor.spam_call_ai_last_summary",
            summary_state,
            {
                "friendly_name": "Spam Call AI Last Summary",
                "icon": "mdi:text-box-outline",
                "full_summary": activity["summary"],
            },
        ),
        (
            "sensor.spam_call_ai_last_callback_number",
            callback_state,
            {
                "friendly_name": "Spam Call AI Last Callback Number",
                "icon": "mdi:phone-return",
            },
        ),
        (
            "sensor.spam_call_ai_last_call_time",
            _iso_utc(activity["time_unix"]),
            {
                "friendly_name": "Spam Call AI Last Call Time",
                "icon": "mdi:clock-outline",
            },
        ),
        (
            "sensor.spam_call_ai_google_sync_status",
            google_status,
            {
                "friendly_name": "Spam Call AI Google Sync Status",
                "icon": "mdi:contacts",
                "last_error": google.get("last_error"),
            },
        ),
        (
            "sensor.spam_call_ai_google_last_sync",
            last_sync,
            {
                "friendly_name": "Spam Call AI Google Last Sync",
                "icon": "mdi:sync",
            },
        ),
        (
            "sensor.spam_call_ai_blocked_callers",
            len(blocked_numbers),
            {
                "friendly_name": "Spam Call AI Blocked Callers",
                "icon": "mdi:phone-off",
                "numbers": blocked_numbers,
                "entries": blocked_entries[-50:],
            },
        ),
    ]

    await _write_ha_states(states)


async def _dashboard_entities_loop() -> None:
    LOGGER.info("Spam Call AI dashboard entity publisher started")
    await asyncio.sleep(6)
    while True:
        try:
            await _publish_dashboard_entities()
        except asyncio.CancelledError:
            return
        except Exception as exc:
            LOGGER.warning("Dashboard entity update failed: %s", exc)
        await asyncio.sleep(10)


async def _publish_safe_contacts_sensor() -> None:
    options = base.load_options()
    names = _safe_contact_names(options)
    safe_numbers = base.trusted_caller_numbers(options)
    google = base.google_contacts_status()

    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        LOGGER.error("Safe contacts sensor: SUPERVISOR_TOKEN is unavailable")
        return

    payload = {
        "state": str(len(safe_numbers)),
        "attributes": {
            "friendly_name": "Spam Call AI Safe Contacts",
            "icon": "mdi:account-check",
            "contacts": names,
            "google_label": google.get("label") or str(
                options.get("google_contact_label") or "Trusted Callers"
            ),
            "google_contact_count": google.get("contact_count", 0),
            "google_number_count": google.get("number_count", 0),
            "google_last_sync_unix": google.get("last_sync_unix"),
            "google_last_error": google.get("last_error"),
            "safe_call_policy": "bypass_ai",
        },
    }

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                "http://supervisor/core/api/states/sensor.spam_call_ai_safe_contacts",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            LOGGER.info(
                "Safe contacts sensor published: entity=sensor.spam_call_ai_safe_contacts count=%s names=%s",
                len(safe_numbers),
                len(names),
            )
    except Exception as exc:
        LOGGER.warning("Could not publish safe contacts sensor: %s", exc)


async def _safe_contacts_sensor_loop() -> None:
    LOGGER.info("Safe contacts sensor publisher started")
    await asyncio.sleep(5)
    while True:
        try:
            await _publish_safe_contacts_sensor()
        except asyncio.CancelledError:
            return
        except Exception as exc:
            LOGGER.warning("Safe contacts sensor update failed: %s", exc)
        await asyncio.sleep(60)


@app.on_event("startup")
async def _start_safe_contacts_sensor() -> None:
    asyncio.create_task(
        _safe_contacts_sensor_loop(),
        name="safe_contacts_sensor",
    )


@app.on_event("startup")
async def _start_dashboard_entities() -> None:
    asyncio.create_task(
        _dashboard_entities_loop(),
        name="spam_call_ai_dashboard_entities",
    )


def _safe_voicemail_twiml(options: dict) -> str:
    max_seconds = int(options.get("safe_voicemail_max_seconds", 120))
    action_url = base.public_base_url(options) + "/trusted/voicemail"
    return (
        "<Say>Sorry, I couldn't reach them. Please leave a message after the tone, "
        "then hang up when you're finished.</Say>"
        f'<Record action={quoteattr(action_url)} method="POST" '
        f'maxLength="{max_seconds}" playBeep="true" />'
        "<Hangup/>"
    )


@app.post("/trusted/dial-result")
async def trusted_dial_result(request: Request) -> Response:
    options = base.load_options()
    form = await request.form()
    form_data = dict(form)

    if not base.validate_twilio_http_request(request, form_data, options):
        raise HTTPException(status_code=403, detail="Invalid Twilio signature")

    status = str(form_data.get("DialCallStatus") or "").lower()
    try:
        duration = int(form_data.get("DialCallDuration") or 0)
    except (TypeError, ValueError):
        duration = 0

    caller = str(form_data.get("From") or "unknown")
    call_sid = str(form_data.get("CallSid") or "")
    safe_state = base.load_json_file(SAFE_CALL_PATH)
    safe_state.update(
        {
            "caller": caller,
            "trusted_name": base.google_synced_name_for_number(caller)
            or str(safe_state.get("trusted_name") or ""),
            "call_sid": call_sid,
            "duration_seconds": duration,
            "updated_at_unix": time.time(),
            "ai_used": False,
        }
    )

    # A normal answered conversation should simply end. Very short "completed"
    # legs are treated like an unanswered forwarding loop and go to voicemail.
    if status == "completed" and duration > 3:
        safe_state["status"] = "answered"
        safe_state["summary"] = "Safe caller connected to your phone. AI was not used."
        base.save_json_file(SAFE_CALL_PATH, safe_state)
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            "<Response><Hangup/></Response>"
        )
    else:
        safe_state["status"] = "voicemail"
        safe_state["summary"] = "Safe caller was not answered and was sent to voicemail. AI was not used."
        base.save_json_file(SAFE_CALL_PATH, safe_state)
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            "<Response>"
            f"{_safe_voicemail_twiml(options)}"
            "</Response>"
        )

    return Response(content=xml, media_type="application/xml")


@app.post("/trusted/voicemail")
async def trusted_voicemail(request: Request) -> Response:
    options = base.load_options()
    form = await request.form()
    form_data = dict(form)

    if not base.validate_twilio_http_request(request, form_data, options):
        raise HTTPException(status_code=403, detail="Invalid Twilio signature")

    caller = str(form_data.get("From") or "unknown")
    trusted_name = base.google_synced_name_for_number(caller)
    data = {
        "caller": caller,
        "trusted_name": trusted_name,
        "call_sid": str(form_data.get("CallSid") or ""),
        "recording_sid": str(form_data.get("RecordingSid") or ""),
        "recording_url": str(form_data.get("RecordingUrl") or ""),
        "recording_duration_seconds": str(
            form_data.get("RecordingDuration") or ""
        ),
        "recorded_at_unix": time.time(),
        "safe_caller": True,
        "ai_used": False,
    }
    base.save_json_file(SAFE_MESSAGE_PATH, data)

    safe_state = base.load_json_file(SAFE_CALL_PATH)
    safe_state.update(
        {
            "caller": caller,
            "trusted_name": trusted_name
            or str(safe_state.get("trusted_name") or ""),
            "call_sid": data["call_sid"],
            "status": "voicemail_recorded",
            "duration_seconds": data["recording_duration_seconds"] or 0,
            "summary": "Safe caller left a voicemail. AI was not used.",
            "recording_sid": data["recording_sid"],
            "recording_url": data["recording_url"],
            "updated_at_unix": time.time(),
            "ai_used": False,
        }
    )
    base.save_json_file(SAFE_CALL_PATH, safe_state)

    LOGGER.info(
        "Safe caller message recorded: caller=%s name=%s duration=%ss",
        caller,
        trusted_name or "manual-safe-number",
        data["recording_duration_seconds"] or "unknown",
    )
    await base.fire_home_assistant_event(
        "spam_call_ai_safe_message_recorded",
        data,
    )

    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        "<Say>Thank you. Your message has been recorded. Goodbye.</Say>"
        "<Hangup/>"
        "</Response>"
    )
    return Response(content=xml, media_type="application/xml")


@app.post("/twiml")
async def twiml_v7(request: Request) -> Response:
    options = base.load_options()
    form = await request.form()
    form_data = dict(form)

    if not base.validate_twilio_http_request(request, form_data, options):
        raise HTTPException(status_code=403, detail="Invalid Twilio signature")

    caller = str(form_data.get("From") or "unknown")
    call_sid = str(form_data.get("CallSid") or "")
    account_sid = str(form_data.get("AccountSid") or "")
    to_number = str(form_data.get("To") or "")

    expected_account = base.required_option(options, "twilio_account_sid")
    if account_sid and account_sid != expected_account:
        raise HTTPException(status_code=403, detail="Unexpected Twilio account")

    caller_norm = base.normalize_phone_number(caller)
    to_norm = base.normalize_phone_number(to_number)

    # Stop a second-ring conditional-forwarding loop.
    if caller_norm and to_norm and caller_norm == to_norm:
        LOGGER.info("Stopped safe-caller re-ring loop for call_sid=%s", call_sid)
        return Response(
            content=(
                '<?xml version="1.0" encoding="UTF-8"?>'
                "<Response><Hangup/></Response>"
            ),
            media_type="application/xml",
        )

    if base.is_trusted_caller(options, caller):
        trusted_name = base.google_synced_name_for_number(caller)
        LOGGER.info(
            "SAFE CALLER matched before AI: caller=%s name=%s",
            caller,
            trusted_name or "manual-safe-number",
        )
        await base.fire_home_assistant_event(
            "spam_call_ai_safe_caller",
            {
                "caller": caller,
                "trusted_name": trusted_name,
                "call_sid": call_sid,
                "ai_used": False,
            },
        )

        destination = str(options.get("forward_to_number") or "").strip()
        rering_enabled = bool(options.get("trusted_rering_enabled", True))

        safe_state = {
            "caller": caller,
            "trusted_name": trusted_name,
            "call_sid": call_sid,
            "status": "re_ringing" if rering_enabled and destination else "voicemail",
            "duration_seconds": 0,
            "summary": (
                "Safe caller bypassed AI and is being re-rung to your phone."
                if rering_enabled and destination
                else "Safe caller bypassed AI and was sent to voicemail."
            ),
            "updated_at_unix": time.time(),
            "ai_used": False,
        }
        base.save_json_file(SAFE_CALL_PATH, safe_state)

        if rering_enabled and destination:
            timeout = int(options.get("trusted_rering_timeout_seconds", 15))
            result_url = base.public_base_url(options) + "/trusted/dial-result"
            dial_xml = (
                f'<Dial answerOnBridge="true" timeout="{timeout}" '
                f'callerId={quoteattr(to_number)} '
                f'action={quoteattr(result_url)} method="POST">'
                f"<Number>{escape(destination)}</Number>"
                "</Dial>"
            )
            xml = (
                '<?xml version="1.0" encoding="UTF-8"?>'
                "<Response>"
                f"{dial_xml}"
                "</Response>"
            )
        else:
            xml = (
                '<?xml version="1.0" encoding="UTF-8"?>'
                "<Response>"
                f"{_safe_voicemail_twiml(options)}"
                "</Response>"
            )

        return Response(content=xml, media_type="application/xml")

    if base.is_blocked_caller(caller):
        LOGGER.info("BLOCKED CALLER rejected before AI: caller=%s", caller_norm)
        await base.fire_home_assistant_event(
            "spam_call_ai_blocked_call_rejected",
            {
                "caller": caller_norm,
                "call_sid": call_sid,
            },
        )
        return Response(
            content='<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>',
            media_type="application/xml",
        )

    # Only non-safe, non-blocked callers enter the AI screening flow.
    xml = _first_screen_gather_twiml(options)
    return Response(content=xml, media_type="application/xml")
