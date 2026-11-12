# Rolling back

Written before it is needed, which is the only time it can be written
calmly. Read the decision rule first; the commands are below it and take a
minute.

**Where this is incomplete, and why.** The runtime host is not chosen yet —
it depends on which telephony provider terminates where (gap 9 in
`RULES.md`). So the *decision* and the *verification* below are final, and
the two lines that actually move traffic are marked `<host>`. Everything
else works today.

---

## What a release is

One immutable image tag, naming the commit it was built from:

```
<registry>/poorvika-voice:main-4f9c2a1b
```

There is no `:latest`. A tag never moves, every tag that has ever been
deployed still exists and still runs, and the process inside reports the
same release on `/healthz`. That is the whole mechanism — rolling back is
starting an older tag, not rebuilding anything.

Everything that decides how the agent behaves is **inside** the image: the
persona, the tool declarations, the context-window settings, the word caps.
They roll back together. See "What rolling back does not undo" for the parts
that do not.

---

## When to roll back, and who says so

Roll back first and diagnose afterwards. The image you are going back to ran
in production and the one in front of you did not.

| Signal | Action |
|---|---|
| Calls end on the first tool call, or mid-sentence | **Roll back now.** This is what a bad context-window setting looks like, and it happened on 2026-11-10 |
| `/readyz` says a capability is missing that was there before | **Roll back now** |
| Error rate over 1 in 20 calls, or rising | **Roll back now** |
| Cost per connected minute over the ₹8 session ceiling | **Roll back now** — the ceiling ends calls, so customers are already being cut off |
| Cost over the ₹2 target but under the ceiling | Do not roll back. That is every release today; see `COST.md` |
| First audio p50 over 1 s, p95 over 2 s | Judgement. Roll back if it is a step change rather than a drift |
| One customer reports one odd answer | Do not roll back. Get the trace |

**Who decides.** Whoever is on call decides to roll back, alone, without
waiting for anyone. Rolling back is cheap and reversible; a bad voice agent
talking to customers is neither. Tell the team after it is done, not before.

**Who decides to roll forward again.** Not the same person, not the same
hour. The fix needs a test that fails without it — the window bug passed a
green suite because nothing compared the trigger against the prompt.

---

## Doing it

### 1. What is actually running

```bash
curl -s https://<host>/healthz
```

```json
{"status": "ok", "env": "main", "release": "4f9c2a1b"}
```

Never take this from a deployment log. A log says what was *asked* for; this
says what answered.

### 2. What to go back to

```bash
git log --oneline -10 main
```

The previous release is the commit before the one `/healthz` reports, unless
two deploys went out together — `deploy.sh` prints the previous tag every
time it runs, so the job log for the bad deploy already names it.

Confirm the image exists before you need it to:

```bash
docker manifest inspect <registry>/poorvika-voice:main-<previous>
```

### 3. Put it back

```bash
# <host>: the one line that depends on where this runs.
#   Cloud Run     gcloud run services update poorvika-voice --image <registry>/poorvika-voice:main-<previous>
#   Kubernetes    kubectl set image deploy/poorvika-voice app=<registry>/poorvika-voice:main-<previous>
#   Plain Docker  docker run -d --env-file /etc/poorvika/voice.env <registry>/poorvika-voice:main-<previous>
```

Calls in flight will drop. A voice session is a held socket and there is no
draining it politely past a few seconds; a caller who is cut off redials,
and that is a better outcome than the thing you are rolling back from.

### 4. Confirm, with the same command as step 1

```bash
curl -s https://<host>/healthz   # release must now be <previous>
curl -s https://<host>/readyz    # every capability true
```

Then **make one real call.** The health endpoint contacts nothing
downstream, on purpose — it cannot tell you the agent works, only that the
process is up.

### 5. Stop the bad release coming back

Revert the commit on `main` rather than leaving the branch ahead of what is
deployed:

```bash
git revert --no-edit <bad-sha>
git push
```

Otherwise the next deploy ships the same image again, which is the most
common way one incident becomes two.

---

## What rolling back does not undo

Worth knowing before you assume the rollback fixed everything.

| | |
|---|---|
| **Host environment variables** | `.env` on the host is not in the image. A release that needed a new setting leaves it set; a release that changed one leaves the new value. Check `/readyz` and the env file together |
| **Poorvika's own services** | The catalogue, pricing, offers and stock are live and upstream. Nothing here caches or writes to them, so there is nothing to undo — and equally, rolling back fixes nothing that is wrong there |
| **Call traces** | `out/traces/` keeps what the bad release wrote. Keep them: they are the evidence |
| **Money already spent** | Gemini bills per turn as it happens |

Nothing in this service writes to a database, because there is no database.
That is what makes a rollback here genuinely safe rather than merely fast,
and it is worth not losing: the day this service owns persistent state, this
page needs a section about migrations.

---

## Before the host is chosen

Two things to settle with it, so this page stops having a `<host>` in it:

1. **Keep at least ten previous tags.** A registry retention policy that
   deletes anything older than a week also deletes the rollback target for
   anything you did not notice within a week.
2. **Health and readiness wired to the load balancer**, with `/readyz`
   gating traffic and `/healthz` gating restarts. They report different
   things on purpose.
