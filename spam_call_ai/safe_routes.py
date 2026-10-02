import asyncio
import logging
import os
import time
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import Response

import app as base

LOGGER = logging.getLogger("spam_call_ai.safe")
app = base.app
SAFE_MESSAGE_PATH = Path("/data/last_safe_message.json")


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
        manual_count = 0
        for token in base.re.split(r"[,;\s]+", manual):
            if base.normalize_phone_number(token):
                manual_count += 1
        if manual_count:
            names.append(f"{manual_count} manual safe number(s)")

    return sorted(names, key=str.casefold)


async def _publish_safe_contacts_sensor() -> None:
    options = base.load_options()
    names = _safe_contact_names(options)
    safe_numbers = base.trusted_caller_numbers(options)
    google = base.google_contacts_status()

    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
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
    except Exception as exc:
        LOGGER.warning("Could not publish safe contacts sensor: %s", exc)


async def _safe_contacts_sensor_loop() -> None:
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

    # A normal answered conversation should simply end. Very short "completed"
    # legs are treated like an unanswered forwarding loop and go to voicemail.
    if status == "completed" and duration > 3:
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            "<Response><Hangup/></Response>"
        )
    else:
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

    # Only non-safe callers enter the AI screening flow.
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        f"{base.ai_stream_xml(options, caller, call_sid, to_number, include_greeting=True)}"
        "<Hangup/>"
        "</Response>"
    )
    return Response(content=xml, media_type="application/xml")
