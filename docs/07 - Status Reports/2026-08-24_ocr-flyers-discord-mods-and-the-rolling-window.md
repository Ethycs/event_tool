# Session Report — flyers get OCR'd, Discord goes client-mod, Telegram rolls

**Date**: 2026-08-24 · **Status**: ✅ LANDED · ⚠️ **9 commits unpushed; two client-mod plugins untested in a live client; OCR off by default**
**Scope**: `/sources` page + onboarding automation · Discord (dead cache → scheduled-events + message-capture client mods) · GLM-OCR on flyer images across sources · Telegram rolling window · the picture/description dyad · browser-side day rollover
**Tests**: 1739 passing · **Lint**: 43, unchanged
**Follows**: [2026-08-13_local-first-llm-and-the-ab-harness.md](2026-08-13_local-first-llm-and-the-ab-harness.md)

---

## 1. Git state (read first)

| | |
|---|---|
| HEAD | `77b6773` |
| origin/main | `668e250` — **9 commits ahead, unpushed** (the user pushes) |
| Tree | clean but for `.mcp.json` (the user's `central-dev-gateway` HTTP entry — theirs to land) |

The nine: OCR flyers (`c047a23`), OCR sources Telegram+Instagram (`3324fa1`), Telegram rolling window (`27b6593`), the dyad (`12440a8`), day rollover (`77b6773`), plus Discord GSE (`c570b18`), Discord capture (`147f175`), the 0-message warning (`b3872b0`), and the dismiss × (`503d4ce`).

---

## 2. GLM-OCR on flyers — built, **off by default**

Event invites are flyers; the date/venue are pixels. The pipeline now OCRs a message's images and folds the text into `content` before extraction.

* **Endpoint** (from the central-dev wiki at `https://central-dev.zt:9000`, read via `GET /api/files/<path>`): GLM-OCR is a **Triton KServe-v2** service, *not OpenAI-shaped*, so it can't use `llm.vision_completion`. `POST http://192.168.1.136:8010/v2/models/glm-ocr/infer`, base64 image in, a JSON list of `{page,bbox,kind,text}` out. Contract + reachability saved to memory **`glm-ocr-endpoint`**.
* **Reachability was the whole fight.** blackbox's Triton ports are ZeroTier-only and this box isn't on ZT; the user opened `.136:8010/:8000` on the LAN mid-session. Verified live: it reads a flyer's title/date/venue (truncates the tail — the model's own `model.py`, not our client).
* **Wiring**: `sources/glm_ocr.py` (client), `pipeline/ocr_enrich.py` (the pass, cached by image hash, additive/non-fatal), a pipeline stage between harvest and filter, `OCRConfig` (`OCR_ENABLED=1`, `GLM_OCR_URL`). Message schema gained an optional `images` field (`EH-STD-MSG-v0`).
* **Producers wired**: Instagram (post image URL), Telegram (photo messages, downloaded when OCR is on, capped 150/run), Discord scheduled-event cover flyers. **Not yet**: web flyers, Gmail images, Discord message attachments — they only need to populate `images`.

**To turn on**: `OCR_ENABLED=1`, then restart the MCP backend (it owns the runner) so the new stage loads.

---

## 3. The dyad — picture and description as one unit

`12440a8`. The flyer and its text were decoupled: OCR fed the text into `content`, but the card's picture came from a link-thumbnail unrelated to it. Now the OCR pass — which holds the bytes — embeds a card thumbnail on the message (`_thumb`); `attach_source_ids` carries it onto the event on a confident single match; it threads through both whitelist projections (`_events_for_report`, then `_prepare`) to the card's `thumb`, and the link-thumbnail step defers to it. The card shows *this* flyer beside *this* description. Needs OCR enabled (that's the pass that captures the pairing).

---

## 4. Discord — the cache is dead, so it went client-mod

**Measured 2026-08-16: 0 of 4,257 desktop-cache entries were `/channels/{id}/messages`.** Discord stopped writing message JSON to the HTTP cache the old reader scraped. Two consequences, both shipped:

* **Scheduled events** (`c570b18`): a BetterDiscord plugin reads `GuildScheduledEventStore` and writes a local JSON file; `sources/discord_events.py` ingests it. Passive read of an in-memory store — the safe shape.
* **Message capture** (`147f175`): a second plugin passively records the messages you load *by browsing* (subscribes to `LOAD_MESSAGES_SUCCESS`/`MESSAGE_CREATE`), zero requests of its own. `scripts/zip_discord_capture.py` bundles them.

> **Both plugins are UNTESTED in a live client** — I can't drive Discord. The user must install BetterDiscord + the two `.plugin.js` files (`integrations/discord-betterdiscord-plugin/`) and check the console line. `read_discord_messages` was refactored so a dead cache no longer early-returns before the scheduled-events merge.

The **ToS honest line**, held throughout: client mods are technically against Discord ToS but unenforced for passive read-only use; bulk message export via the user token (DiscordChatExporter) is self-botting with real ban risk and was **not** built. Where the user has Manage Server, the bot API is the clean path.

---

## 5. Telegram rolling window — with one real caveat

`27b6593`. `cutoff = max(now - DAYS_BACK, last_harvest - overlap)`: a daily run fetches ~30h (24 + 6h overlap) instead of re-scanning 7 days. `DAYS_BACK` is the ceiling (first run / post-outage), state in `data/.telegram_window.json`, advanced only after a clean read. On by default; `TELEGRAM_ROLLING_WINDOW=0` restores the fixed window.

> **⚠️ Caveat flagged to the user, still open.** `_save_latest_events` **replaces**, not merges. An event is on the site only while its source message is in the window — so narrowing the window shortens how long a Telegram event stays visible (≈30h vs the old ≈7 days), even for a far-future event. The right companion is **event accumulation** (keep upcoming events across runs, prune past, respect declines). Not built. Until it is, either accept the ~30h review horizon or raise `TELEGRAM_OVERLAP_HOURS`.

---

## 6. Also shipped

* **`/sources` page** (`668e250`, pushed): configured sources grouped by how they must be *run* — direct / search / special — plus a browser-free "examine a URL" flow (`sources/onboard.py`) that finds the better URL and refuses to guess without a signal (`needs_browser`), and an agent offramp (`onboard_handoff.py`) that hands over a brief, not a URL. Every attempt is logged.
* **0-message warning** (`b3872b0`): an enabled source that harvests nothing now says so — a dead source stops looking like a quiet night. This is what would have surfaced dead Discord on its own.
* **Dismiss ×** (`503d4ce`): a red × per tile, localStorage-scoped, Undo + restore-all.
* **Day rollover** (`77b6773`): the page computes Today/Tomorrow/… in the browser against the viewer's local midnight, so a page served for 24h doesn't keep yesterday's labels. Was found complete-but-uncommitted in the tree; landed intact.

---

## 7. Open, in priority order

1. **Restart the MCP backend** to load: the funnel split, the OCR stage, the Telegram download + rolling window, and the dyad threading. It still runs pre-`c995142` code. This is the one restart that costs a harvest.
2. **Event accumulation** — the companion the Telegram rolling window needs (§5). Touches the approve/decline machinery; do it carefully.
3. **Install & verify the two Discord plugins** live (§4) — the only way to know the Flux store shapes match this build.
4. **Wire the remaining OCR producers**: web flyers (richest), Gmail images, Discord attachments. Each just populates `images`.
5. **`llm_no_events` accounting hole** (from `c995142`): the extract stage still buckets per batch, so a partial batch under-attributes. The filter stages are fixed and checked; extract is not (see `01 - Design/04 - Decomposing the Pipeline.md` §4.5).
6. **Rebuild the served pages** so the day-rollover and dyad reach the live site (`event-harvester site --build-only --no-harvest-on-start`).
7. **ZeroTier**: this box isn't on the overlay, so GLM-OCR works only via the LAN address the user opened. Joining ZT would restore `10.158.218.141:8010` as the durable path.

---

## Verification

```powershell
pixi run test                                   # 1739 passed
pixi run lint                                   # 43, unchanged
git log --oneline origin/main..HEAD             # 9 unpushed
# OCR reachable? (LAN)
pixi run python -c "from event_harvester.sources.glm_ocr import health; print(health())"
```

**First action next session**: check `pixi run event-harvester jobs list`, then decide whether to restart the MCP backend (open item 1) — nothing new reaches production until it does.
