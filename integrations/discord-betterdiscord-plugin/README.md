# HarvesterScheduledEvents — a BetterDiscord plugin

Exports the guild **scheduled events** your Discord client already knows about
to a local JSON file, so the event harvester can pick them up. This is the
member-side path: it works on any guild you are in, no *Manage Server* needed.

## What it does, and what it deliberately does not

- **Reads** `GuildScheduledEventStore` from Discord's in-memory Flux store.
- **Writes** one file: `%APPDATA%\discord-harvester\scheduled-events.json`
  (Windows) or `~/.config/discord-harvester/scheduled-events.json`.
- Re-writes it on the relevant Flux events and every 15 minutes.

It sends no message, makes no network request, and automates nothing about your
account. That matters: client mods are technically against Discord's ToS
(modifying the client), and Discord's stated position is that it does not action
users for BetterDiscord/Vencord-style mods **unless they are used to abuse the
platform** — self-bots, automation, spam. A read-only local export is the
least-exposed shape a client mod can take, but it is still a client mod. Run it
with that understood.

## The supported alternative

If you have **Manage Server** on the guilds you care about, a bot with the
`GUILD_SCHEDULED_EVENTS` intent and `GET /guilds/{id}/scheduled-events` is fully
ToS-compliant and needs no client mod. Use that where you can; this plugin is
for guilds where you are only a member.

## Install

1. Install BetterDiscord: https://betterdiscord.app/
2. Copy `HarvesterScheduledEvents.plugin.js` into your plugins folder:
   - Windows: `%APPDATA%\BetterDiscord\plugins\`
   - macOS: `~/Library/Application Support/BetterDiscord/plugins/`
   - Linux: `~/.config/BetterDiscord/plugins/`
   (Discord → Settings → Plugins → **Open Plugins Folder** does this for you.)
3. Enable it in Settings → Plugins.

## Verify it is working

1. After enabling, open the console: `Ctrl+Shift+I` → Console. You should see
   `[Harvester] scheduled-event export -> …\scheduled-events.json` and
   `[Harvester] wrote N scheduled event(s)`.
2. Check the file exists and has your events:
   ```powershell
   type "$env:APPDATA\discord-harvester\scheduled-events.json"
   ```
3. From the harvester, confirm it ingests them:
   ```powershell
   pixi run python -c "from event_harvester.sources.discord_events import read_scheduled_events as r; print(len(r()), 'event(s)')"
   ```
   or run a scoped harvest:
   ```powershell
   pixi run event-harvester --only discord --days 30 --dry-run --no-sync
   ```

## If it finds nothing

- `GuildScheduledEventStore not found` toast → Discord changed the store name;
  the plugin needs updating. Open an issue with your Discord build number.
- File written but `0 scheduled event(s)` → none of your guilds have upcoming
  events, or they are all in the past (past and cancelled events are dropped by
  the harvester, not the plugin).
- The harvester reads `%APPDATA%\discord-harvester\...` by default. If you moved
  it, set `DISCORD_EVENTS_EXPORT` to the full file path on the harvester side.

## Status

**Untested in a live client at time of writing** — authored against Discord's
documented Flux store and event names, defensively (it tries several store
method shapes). The harvester-side reader *is* covered by offline tests. If the
store shape differs on your build, the console line and the toast will say so.
