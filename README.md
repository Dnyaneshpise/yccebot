<<<<<<< HEAD
# AWS Builder Badge Agent

A free, self-hosted agent that tracks your **AWS Builder Center badge progress**
against the 7 / 14 / 21-badge AWS Student Rewards milestones and sends you a
daily summary on Telegram. It runs on GitHub Actions, so there is no server to
pay for.

> ### This agent does not publish anything
> It **reads** your dashboard, **tracks** badges, and **drafts** content for you
> to review. It never posts, comments, upvotes, or likes on your behalf, and it
> never invents experiments, deployments, or personal experience. You always
> press publish yourself.

---

## What it does each day

1. Decodes your saved AWS session and opens Builder Center with Playwright.
2. Verifies you are still signed in (and warns you if the session expired).
3. Reads your current badge count and updates `data/progress.json`.
4. Sends a Telegram message with progress, the next milestone, and what is left.
5. Celebrates milestone crossings at **7**, **14**, and **21** badges.
6. Optionally discovers relevant public articles and drafts a substantive
   comment for **your review** - never auto-posts.

---

## Architecture

```
scripts/save_login_state.py  ->  auth/storage_state.json   (local only, gitignored)
scripts/encode_state.py      ->  base64 string             -> GH secret
                                        |
.github/workflows/badge-agent.yml  (cron: 30 3 * * *  =  03:30 UTC daily)
                                        |
                                 python -m agent.main
                                        |
   +-------------+   +-----------+   +---+--------+   +-----------+   +-----------+
   |  builder.py |   |  ai.py    |   |  rewards   |   | discovery |   | telegram  |
   |  Playwright |   | OpenRouter|   |  milestones|   |  ranking  |   |  Bot API  |
   +------+------+   +-----+-----+   +-----+------+   +-----+-----+   +-----+-----+
          |                  |                 |                 |               |
          +------------------+--------+--------+-----------------+---------------+
                                        |
                              data/progress.json  (committed back to the repo)
```

| Module | Responsibility |
| --- | --- |
| `agent/config.py` | Environment-driven settings, validation, path resolution |
| `agent/builder.py` | Playwright browser, auth check, badge scraping, activity discovery |
| `agent/rewards.py` | 7/14/21 milestone logic, badge parsing, rewards disclaimer |
| `agent/storage.py` | Atomic JSON progress store with a normalised schema |
| `agent/ai.py` | OpenRouter chat completions (summarize, classify, draft) |
| `agent/discovery.py` | Relevance scoring and noise filtering |
| `agent/telegram.py` | Bot API notifications with automatic secret redaction |
| `agent/main.py` | Orchestration and CLI |

### Design notes

- **Never guesses.** If the badge count cannot be read, the run records nothing
  and keeps your previous value. `parse_badge_count` returns `None` rather than
  inventing a number.
- **Resilient selectors.** Builder Center markup is not a stable API, so every
  lookup falls back from specific selectors to generic ones to plain-text
  parsing. Every failure path returns `None` / `[]`.
- **Graceful degradation.** A missing OpenRouter key or Telegram token produces
  warnings, not crashes - badge tracking still runs.
- **Secrets are redacted.** Tokens and chat IDs are stripped from any text
  before it is sent to Telegram or written to an error message.

---

## Local setup

Requires **Python 3.12+**.

```bash
git clone <your-repo-url> aws-builder-badge-agent
cd aws-builder-badge-agent

python -m venv .venv
# Windows:   .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate

pip install -r requirements.txt
python -m playwright install chromium
```

Copy the example environment file and fill it in:

```bash
cp .env.example .env      # Windows: copy .env.example .env
```

| Variable | Required | Purpose |
| --- | --- | --- |
| `AWS_STORAGE_STATE_B64` | **Yes** | Base64 Playwright session (GitHub secret) |
| `OPENROUTER_API_KEY` | No | Enables summaries and drafts |
| `OPENROUTER_MODEL` | No | Any OpenRouter model ID (default: free Gemini Flash Lite) |
| `TELEGRAM_BOT_TOKEN` | No | Enables Telegram notifications |
| `TELEGRAM_CHAT_ID` | No | Target chat |
| `BUILDER_URL` | No | Defaults to `https://builder.aws.com/` |
| `HEADLESS` | No | `true` by default |
| `DRY_RUN` | No | `true` = track badges only, never draft |
| `MAX_DRAFTS` | No | Max drafts per run (default `3`) |
| `MIN_RELEVANCE` | No | Relevance threshold (default `4`) |
---

## Step 1 - Create your AWS session (local, one time)

This must run on your own machine - it needs a browser and your login.

```bash
python scripts/save_login_state.py
```

A Chromium window opens. Sign in to `builder.aws.com`, wait for your dashboard
to render, then press **Enter** in the terminal. The session is written to
`auth/storage_state.json` (gitignored, mode `600`).

## Step 2 - Encode it for GitHub Actions

```bash
python scripts/encode_state.py
```

This prints a single-line base64 blob. Copy it between the markers.

## Step 3 - Create the GitHub repository

```bash
cd aws-builder-badge-agent
git init
git add .
git commit -m "feat: AWS Builder Badge Agent"
gh repo create aws-builder-badge-agent --private --source=. --push
```

Use `--private` unless you want your progress history public.

## Step 4 - Add the secrets

```bash
# Session (required)
gh secret set AWS_STORAGE_STATE_B64 < path/to/encoded.txt

# OpenRouter (optional - enables drafting)
gh secret set OPENROUTER_API_KEY

# Telegram (optional - enables notifications)
gh secret set TELEGRAM_BOT_TOKEN
gh variable set TELEGRAM_CHAT_ID
```

| Name | Type | Required |
| --- | --- | --- |
| `AWS_STORAGE_STATE_B64` | Secret | **Yes** |
| `OPENROUTER_API_KEY` | Secret | No |
| `OPENROUTER_MODEL` | Variable | No |
| `TELEGRAM_BOT_TOKEN` | Secret | No |
| `TELEGRAM_CHAT_ID` | Variable | No |

> `TELEGRAM_CHAT_ID` and `OPENROUTER_MODEL` are configured as repository
> *variables* (not secrets) so they are visible in the workflow file for easy
> debugging. Use a secret instead if you prefer.

## Step 5 - First run

```bash
gh workflow run badge-agent.yml
gh run watch
```

Then confirm the schedule is registered:

```bash
gh api repos/:owner/:repo/actions/workflows/badge-agent.yml
```

Daily at **03:30 UTC**. GitHub disables scheduled workflows after 60 days of
repository inactivity, so occasionally open the repo or run it manually.

---

## Getting your Telegram chat ID

1. Message [@BotFather](https://t.me/BotFather), send `/newbot`, copy the token.
2. `gh secret set TELEGRAM_BOT_TOKEN` with that token.
3. Send any message to your new bot.
4. Open `https://api.telegram.org/bot<TOKEN>/getUpdates` and read
   `"chat":{"id": ...}`.
5. `gh variable set TELEGRAM_CHAT_ID` with that number.

---

## Local commands

```bash
python -m agent.main --check-config   # validate config, touch nothing
python -m agent.main --no-draft       # badge tracking only
python -m agent.main --draft          # tracking + review-only drafts
python -m pytest tests/ -q            # run the test suite
```

---

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `no AWS session found` | Secret missing or not decoded | Re-run steps 1-2 |
| `session is not authenticated` | Session expired or wrong account | Re-run `save_login_state.py`, update the secret |
| `AWS_STORAGE_STATE_B64 is not valid base64` | Truncated copy | Re-run `encode_state.py` and paste the whole line |
| `badge count could not be read` | Selector change on AWS side | Check the run log; previous value is kept |
| `OpenRouter HTTP 404` | Free model retired | Set a different `OPENROUTER_MODEL` |
| `OpenRouter HTTP 429` | Free tier rate limited | Wait, or lower `MAX_DRAFTS` |
| `Telegram API error: 401` | Wrong bot token | Re-check with @BotFather |
| Workflow never runs on schedule | Repo inactive > 60 days | Run it manually once |

---

## Responsible use

- Only participate in activities you actually understand.
- Never claim personal experience you do not have - the AI is explicitly
  instructed to use conditional phrasing instead.
- Never post engagement bait or ask for likes.
- Milestone reward values are informational and **not guaranteed**; AWS can
  change the terms. Always confirm on the
  [official AWS Student Rewards page](https://aws.amazon.com/education/aws-student-rewards/).
- This tool automates *your own* account only. Do not use it to game the
  program or to post on behalf of others.
=======
# yccebot
>>>>>>> origin/main
