# Deploying Vera+ (Render Free + keep-alive)

## 1. Create the web service
Render dashboard → **New → Web Service** → connect `suryansh-851/magicpin-vera-ai-challenge`.

| Field | Value |
|---|---|
| Runtime | Python 3 |
| Build command | `pip install -r requirements.txt` |
| Start command | `uvicorn bot:app --host 0.0.0.0 --port $PORT --workers 1` |
| Instance type | Free |
| Health check path (Advanced) | `/v1/healthz` |

`--workers 1` is required: all state (contexts, conversations, opt-outs) lives in one process's memory.

## 2. Environment variables
| Key | Value |
|---|---|
| `LLM_PROVIDER` | `groq` |
| `LLM_API_KEY` | your Groq key (never commit it) |
| `PYTHONUTF8` | `1` |
| `PYTHON_VERSION` | `3.12.7` |
| `TEAM_NAME` | `Vera+` |
| `TEAM_MEMBERS` | `Suryansh Singh` |

Leave `LLM_COMPOSE` unset unless the README says it is on (see "Composer" there).

## 3. Keep it awake (required on the Free plan)
Free instances sleep after ~15 minutes without traffic. A sleeping bot takes 30–60 s to wake, the judge's
healthz times out after a few seconds, and 3 consecutive failures disqualify the slot. A restart also wipes
every context the judge pushed.

- **Primary:** create a free monitor at uptimerobot.com (or cron-job.org): HTTP(s), URL
  `https://<your-service>.onrender.com/v1/healthz`, interval **5 minutes**.
- **Backup:** in GitHub → repo **Settings → Secrets and variables → Actions → Variables**, add
  `BOT_URL` = `https://<your-service>.onrender.com`. The `keep-alive` workflow then pings every 10 minutes.

Before submitting, and again just before the test window, open `/v1/healthz` yourself and confirm it answers
instantly with `"status":"ok"`.

## 4. Verify the live URL
```bash
curl https://<your-service>.onrender.com/v1/healthz
curl https://<your-service>.onrender.com/v1/metadata
python scripts/contract_test.py https://<your-service>.onrender.com   # needs dataset/expanded locally
```
`contract_test.py` ends with `/v1/teardown`, which wipes the bot's state — that's fine before the test,
the judge re-pushes everything during warmup.

## Known limits of this setup
- Groq free tier allows ~8,000 tokens/minute **and 200,000 tokens/day per model (rolling 24 h)**. Under load
  the bot falls back to deterministic templates instantly, so it never times out, but fewer replies are
  LLM-written. **Do not run `scripts/self_judge.py` or `judge_simulator.py` with the bot's key in the 24 hours
  before the test** — a full self-judge pass uses most of the daily budget. Use a different model
  (e.g. `JUDGE_MODEL=openai/gpt-oss-20b`) or a different key for local scoring.
- Render Free has 0.1 CPU. Templates compose in milliseconds, so the 30 s budget is not at risk.
- If Render restarts the instance mid-test, in-memory state is lost. The keep-alive prevents idle restarts;
  deploys also restart it, so don't push changes during the test window.

## Tests
```
python scripts/smoke_test.py          # composes every trigger offline, checks the validator
python scripts/regression_test.py     # targeted checks: commit artifacts, opt-out scoping, expiry, fabrication
python scripts/contract_test.py <url> # full HTTP contract against a running server
```

## Package for submission
```
python scripts/package_submission.py [out.zip]
```
Builds the ZIP from an explicit allow-list (never a blind directory copy) and refuses to write it if a
secret-shaped token is found in a tracked file. Excludes `.env`, `*.llm_cache.json`, `*.judge_cache.json`,
`__pycache__/`, and the regenerable `dataset/expanded/`.
