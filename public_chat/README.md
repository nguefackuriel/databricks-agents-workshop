---
title: Northline Cycles Assistant
emoji: 🚲
colorFrom: green
colorTo: gray
sdk: gradio
sdk_version: 6.30.0
app_file: app.py
pinned: false
---

# Northline Cycles public chat

A public chat for the Northline Cycles Customer Care Genie Agent built in the Databricks agents workshop. Anyone can use it without a Databricks account: the app calls the Genie Conversation API server-side with a token kept in this Space's secrets.

## Configure

In **Settings → Variables and secrets**:

| Name | Type | Value |
| --- | --- | --- |
| `DATABRICKS_HOST` | Variable | Your workspace URL, for example `https://dbc-1234abcd-5678.cloud.databricks.com` |
| `GENIE_SPACE_ID` | Variable | The Genie Agent ID (Genie Agents → Northline Customer Care → Configure → About) |
| `DATABRICKS_TOKEN` | Secret | A token for a user or service principal that can run the agent |

Optional: `MAX_MESSAGES_PER_SESSION` (default 15) and `MAX_MESSAGES_PER_DAY` (default 300), which protect your Free Edition daily quota, `GENIE_MODE`, and `GUARDRAILS` (default `on`).

## Agent mode or chat mode

By default the app uses Genie **Agent mode**, the same as the Agent toggle in the Genie UI: the agent plans, calls its tools and writes an answer. If Agent mode is not enabled for the workspace, the app falls back to **Chat mode** (one SQL query, results shown as a table). Set `GENIE_MODE=chat` to force Chat mode.

## Guardrails

The app checks every exchange before it reaches the visitor:

- **Input check** (before calling Databricks): blocks prompt-injection phrasing ("ignore your instructions", "you are now…", "system prompt"), business questions (revenue, sales totals, how many customers), bulk requests (list all customers, every email, SQL), and off-topic first questions. A blocked question never reaches the agent, so it costs no quota.
- **Output check** (before showing the answer): withholds an answer that mentions more than one other customer ID, more than three other order IDs, a table longer than six rows, or business figures.

A blocked question gets "I can only help with one specific order or customer…"; a withheld answer gets "Sorry, that answer contained more customer or business data…", so you can tell which check fired. Both are logged as `[guardrail]` in the Space logs with the reason. Set `GUARDRAILS=off` to turn both checks off, for example to show the leak during a demo.

These checks are heuristics: a determined visitor can rephrase around them. They are defense in depth, not the security boundary.

## Security

- The token never reaches the browser, but the app answers with the token owner's access to every source attached to the agent. Point `GENIE_SPACE_ID` at a **public agent** that has tools (SQL functions) and public text only, not raw tables: instructions cannot stop a prompt injection, missing sources can.
- Anyone can ask the agent questions. Only use synthetic or public data.
- Prefer a service principal that can only run this agent and read the `agents_workshop` schema. If you use a personal access token, give it a short lifetime and revoke it after the event.
