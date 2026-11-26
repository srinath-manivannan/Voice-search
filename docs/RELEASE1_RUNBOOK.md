# Release 1 launch runbook

This release is the Chat widget only. The process runs with `APP_MODE=widget`. Voice (`/ws`) and Guided (`/api/v1/guided/start`) are not mounted, and paid live voice cannot be turned back on in this mode.

Names used below match the settings the process reads: `chat.paid_enabled` is `CHAT_PAID_ENABLED`, and `widget.enabled` is `WIDGET_ENABLED`.

## 1. Rotate upstream keys before anything else

Every upstream key that has been pasted into a chat, a ticket, or a shared page is treated as public. Issue new keys, put them only in the secret store, and restart the process. Then call one upstream with an old key and confirm it is rejected. Do this before the staff canary, not after.

## 2. Check the build on a copy that has no shoppers

From a clean checkout:

```bash
docker compose -f docker-compose.widget.yml up --build
```

Open `http://localhost:8088`. The page is a sample product, Demo Handset A1, with a variant switcher and the launcher.

- Open the assistant. The English welcome is already on screen. No microphone prompt.
- Tap **Is this available?** The 256 GB colour is out of stock; the 128 GB colour is in stock nearby.
- Change the variant and ask again. The answer follows the variant on the page.
- Tap **‹ Back to product**. The panel closes and the page returns to the product.

Then run the canary against that copy. The demo has no live catalogue token, so `/readyz` is not fully ready:

```bash
python scripts/verify_release1_canary.py \
  --url http://localhost:8080 \
  --host-origin http://localhost:8088 \
  --allow-unready
```

A copy that will take shopper traffic omits `--allow-unready`. `/readyz` must say `"ready": true`.

## 3. Roll out in steps

Move traffic only at these stops. Do not skip one.

| Stop | Who is on it | Move on only when |
|---|---|---|
| Staff canary | Staff accounts, one storefront origin | The canary script passes. Opening the panel creates no socket and no microphone prompt. One send produces one reply. `neutrality_block_total` stays 0. |
| 1% | A small slice of shoppers | The same checks, held long enough to see a quiet hour. No stuck composer. Spend stays inside the daily ceiling. |
| 5% | Wider slice | Error rate is not climbing. `/ws` and `/api/v1/guided/start` still return 404. |
| 25% | Quarter of traffic | The 5% checks still hold. No rollback trigger from `docs/ROLLBACK.md`. |
| 100% | Everyone | The 25% checks still hold. |

At every stop, `/healthz` shows the release you meant to ship. If it shows something else, you are not looking at the copy you think you are.

The storefront origin in `WIDGET_HOST_ORIGINS` is an exact scheme, host and port. Add the next origin only when that stop starts. A page that is not on the list cannot embed the widget, and a socket whose `Origin` is not this service is closed with 1008.

## 4. Kill switches

Use these before a rollback when the widget is the problem and the rest of the site is not.

| Switch | Set | What shoppers see | How fast |
|---|---|---|---|
| `chat.paid_enabled=false` | `CHAT_PAID_ENABLED=0` | The next message gets the unavailable line. No model call is made. | The next request. This is the switch that stops spend. |
| `widget.enabled=false` | `WIDGET_ENABLED=0` | The launcher script becomes a no-op and `/ws/chat` refuses with 1013. The panel route returns 404. | The next uncached load of the launcher. A live launcher is cached for 60 seconds, so turn `chat.paid_enabled` off as well when spend must stop now. |

Turning either switch back on is the same change in reverse. Confirm with one staff session before you let the percentage climb again.

## 5. Roll back and prove it

Follow `docs/ROLLBACK.md`: start the previous image tag, do not rebuild it. Then prove the rollback, in this order:

1. `GET /healthz` returns `"status": "ok"` and the `release` of the image you rolled back to.
2. Run `scripts/verify_release1_canary.py` against the restored process. It must pass, including `/ws` and `/api/v1/guided/start` returning 404, the widget `frame-ancestors` and `Permissions-Policy` headers, no `X-Frame-Options` on `/widget/v1/`, a 1008 close for a foreign socket origin, and one reply for a repeated message id.
3. Open the staff storefront, open and close the panel, and send one message. The composer must be usable again afterwards.

If step 2 fails, the restored process is not the Release 1 widget. Stop traffic with `chat.paid_enabled=false` and `widget.enabled=false` until it is.
