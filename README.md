# Build a Team of AI Agents with Databricks

Files for the hands-on workshop **"Build a Team of AI Agents with Databricks: From Your Data to a Live Chatbot"** (Zindi Ambassador workshop, November 7, 2026, online).

In two hours, on a free Databricks account, you build:

- a **Sales Analyst** agent that answers questions about a shop's data,
- a **Customer Care** agent that checks returns and refunds with three tools,
- a **router** (Genie One) that sends each question to the right agent,
- a **public chatbot** on a web page, with simple guardrails.

The shop, Northline Cycles, is made up. All data is synthetic.

## What is in this repo

| File | What it is |
| --- | --- |
| `participant_guide.md` | The step-by-step guide for Labs 0 to 4, plus troubleshooting and bonus challenges |
| `00_northline_setup.ipynb` | The setup notebook. Import it in Databricks and click **Run all** (Lab 0) |
| `northline_website.html` | The fictional Northline web page with the chat bubble (Lab 4) |
| `public_chat/` | The small Gradio app that puts the agent on the web through a Hugging Face Space (Lab 4) |

## Before you start

1. Create a free Databricks account: https://www.databricks.com/learn/free-edition (no credit card).
2. Download `00_northline_setup.ipynb` from this repo (click the file, then **Download raw file**).
3. For Lab 4, create a free Hugging Face account: https://huggingface.co/join

## Quick start (Lab 0)

1. In Databricks, open **Workspace**, then your user folder.
2. Click **⋮ → Import → File** and pick `00_northline_setup.ipynb`.
3. Open the notebook and click **Run all**. Leave the widgets as they are (catalog = `workspace`). It takes 3 to 5 minutes.
4. The last cell should show 5 demo orders and Maya Chen with the **Gold** tier. You are ready for Lab 1.

Then follow `participant_guide.md`.

## The public chatbot (Lab 4)

The live demo runs here: https://huggingface.co/spaces/uriel/northline-assistant

To build your own, see `public_chat/README.md`. In short: clone your Customer Care agent into a **public agent with tools only** (no customer or order tables), create a Gradio Space on Hugging Face, upload the three files from `public_chat/`, add your workspace URL, the agent ID and a short-lived token in the Space settings, then paste the Space URL into `CHAT_URL` at the top of `northline_website.html`.

Keep in mind: instructions guide, guardrails filter, sources enforce. A public agent should only have access to what it is allowed to share.

## Links

- Event page: https://luma.com/kui2s2zm
- Databricks Free Edition: https://www.databricks.com/learn/free-edition
- Genie Agents docs: https://docs.databricks.com/aws/en/genie-agents

Uriel Nguefack Yefou · Senior AI/ML Engineer · Zindi Country Ambassador (Cameroon)
