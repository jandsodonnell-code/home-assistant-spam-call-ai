# AI Home Status Assistant — v1

An on-demand report of selected Home Assistant entities, with a dashboard button,
AI summary and a basic fallback report. This is a script blueprint, not an app
in the app store. It runs in Home Assistant and is maintained through GitHub.
Requires Home Assistant 2025.7 or later and a current version is recommended.

## Install

1. In Home Assistant open **Settings → Automations & scenes → Blueprints → Import blueprint**.
2. Paste:
   `https://github.com/jandsodonnell-code/home-assistant-spam-call-ai/blob/main/ai_home_status/home_status.yaml`
3. Open the imported **AI Home Status Assistant** blueprint and create a script.
4. Select the entities you want in the report. Start with people, a measured
   indoor-temperature sensor or climate entity, lights, fan, media players and
   an away-comfort automation. Add doors and meal helpers only if you have them.
5. Leave AI Task empty for the initial basic test, or choose an `ai_task.*` entity.
6. Save the script as **AI Home Status Assistant**. Check its entity ID is
   `script.ai_home_status_assistant`; if not, edit the dashboard card to match it.
7. Run it. A Home Assistant notification should contain the report.

## Enable AI

Use an AI integration that supplies an **AI Task** entity. For OpenAI, go to
**Settings → Devices & services → Add integration → OpenAI**, enter your API key
locally, then add an AI Task subentry if the integration does not already have one.
Turn off **Control Home Assistant** and web search for the status task. Select
that `ai_task.*` entity in this script's blueprint settings. A `conversation.*`
entity is not an AI Task entity. API usage is billed by your chosen provider.

The spam-call app's saved API key is not automatically available to the native
OpenAI integration. Enter it in Home Assistant, never in this repository.

## Dashboard

1. Add `status_sensor.yaml` to the existing `template:` list in
   `/config/configuration.yaml`. Do not create a second `template:` key or
   overwrite your existing templates. If there is no template key, add it and
   indent the supplied list beneath it. If you use `template: !include ...`,
   append the entry to that included file instead.
2. Check configuration in Developer Tools → YAML, then reload template entities
   (or restart Home Assistant if the template integration was newly added).
3. Add a manual dashboard card and paste `dashboard_card.yaml`.
4. Run the script again. Confirm the sensor ID is `sensor.ai_home_status`; if HA
   assigned a suffix, adjust the card to that ID.

The timestamp is the snapshot time in your Home Assistant timezone. Reports are
not continuously refreshed; a restored report may be old after a restart.

## Behavior and limitations

- Only selected entity states and a small attribute allowlist are sent to AI.
- No GPS coordinates, contact lists, credentials or camera images are collected.
- Presence state can contain a zone name; select only entities you want shared
  with your chosen AI provider.
- AI errors or an empty AI response produce a basic report. A failure may still
  appear in the script trace; the report continues. A provider timeout can delay
  fallback until the integration finishes its request.
- Report text is an attribute rather than the sensor state, avoiding HA's
  255-character state limit.
- No schedules or push notifications are enabled in v1; reports run on demand.
- A selected automation's `on` state means enabled, not successful execution.
- Missing entities are reported as unknown. Absence of an alert is not proof
  everything in the house is safe. AI summaries may need correction; the source
  entity states remain authoritative.
- A feeding checkbox reports recorded feeding, not observed animal behavior.
- Updating a GitHub file does not automatically update the installed blueprint.
  Re-import the blueprint to receive changes and reload scripts if needed.

## Validation

YAML and Jinja templates are checked locally using representative mock states,
including unavailable/missing entities, Celsius/Fahrenheit climates and AI
failure/empty response. Installation and live AI behavior must be verified in
Home Assistant; this project was not tested against your running instance.

## Official references

- https://www.home-assistant.io/integrations/ai_task/
- https://www.home-assistant.io/integrations/openai_conversation/
- https://www.home-assistant.io/docs/blueprint/schema/
- https://www.home-assistant.io/integrations/template/
