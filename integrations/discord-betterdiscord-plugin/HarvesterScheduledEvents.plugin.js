/**
 * @name HarvesterScheduledEvents
 * @author eventharvester
 * @description Reads guild scheduled events from Discord's in-memory Flux store and writes them to a local JSON file for the event harvester. Read-only: it never sends a message, makes no network request, and automates nothing — it only reads a store and writes one file.
 * @version 1.0.0
 * @source https://github.com/local/event_tool
 */

// ── Why this exists ─────────────────────────────────────────────────────────
//
// Discord stopped writing message JSON to its desktop HTTP cache, and guild
// scheduled events were never in that cache at all — they live only in the
// running client's `GuildScheduledEventStore`. The supported reads are a bot
// with the GUILD_SCHEDULED_EVENTS intent (needs Manage Server to invite) or a
// client mod that reads the store from inside the renderer. This is the second.
//
// It writes to  %APPDATA%\discord-harvester\scheduled-events.json  (Windows) or
// ~/.config/discord-harvester/scheduled-events.json  elsewhere. The harvester's
// `sources/discord_events.py` reads exactly that file. Override the harvester
// side with DISCORD_EVENTS_EXPORT if you move it.

const fs = require("fs");
const path = require("path");
const os = require("os");

const EXPORT_DIR = path.join(
    process.env.APPDATA || path.join(os.homedir(), ".config"),
    "discord-harvester"
);
const EXPORT_FILE = path.join(EXPORT_DIR, "scheduled-events.json");

// Flux events that mean the scheduled-event set may have changed. CONNECTION_OPEN
// catches the initial hydration after login; the periodic flush is the backstop.
const WATCH = [
    "GUILD_SCHEDULED_EVENT_CREATE",
    "GUILD_SCHEDULED_EVENT_UPDATE",
    "GUILD_SCHEDULED_EVENT_DELETE",
    "GUILD_SCHEDULED_EVENTS_UPDATE",
    "CONNECTION_OPEN",
];

module.exports = class HarvesterScheduledEvents {
    start() {
        this._subs = [];
        this.store = BdApi.Webpack.getStore("GuildScheduledEventStore");
        this.guildStore = BdApi.Webpack.getStore("GuildStore");
        this.dispatcher = BdApi.Webpack.getByKeys("dispatch", "subscribe");

        if (!this.store) {
            BdApi.UI.showToast("Harvester: GuildScheduledEventStore not found", { type: "error" });
            console.error("[Harvester] GuildScheduledEventStore not found");
            return;
        }

        this._onChange = () => this._debounce();
        for (const ev of WATCH) {
            try {
                this.dispatcher.subscribe(ev, this._onChange);
                this._subs.push(ev);
            } catch (e) { /* an unknown event name is not fatal */ }
        }

        this.flush();                                    // once now
        this._timer = setInterval(() => this.flush(), 15 * 60 * 1000);  // and every 15 min
        console.log("[Harvester] scheduled-event export -> " + EXPORT_FILE);
    }

    stop() {
        if (this._timer) clearInterval(this._timer);
        if (this._deb) clearTimeout(this._deb);
        for (const ev of (this._subs || [])) {
            try { this.dispatcher.unsubscribe(ev, this._onChange); } catch (e) { /* */ }
        }
    }

    _debounce() {
        clearTimeout(this._deb);
        this._deb = setTimeout(() => this.flush(), 2000);
    }

    // Discord's internal store has changed method names across versions, so try
    // the known shapes in turn rather than betting on one. Whatever is found is
    // deduplicated by id.
    _collect() {
        const s = this.store;
        const raw = [];
        try {
            if (typeof s.getAllGuildEvents === "function") {
                const byGuild = s.getAllGuildEvents() || {};
                for (const list of Object.values(byGuild)) raw.push(...(list || []));
            } else {
                const ids = (this.guildStore && this.guildStore.getGuildIds)
                    ? this.guildStore.getGuildIds() : [];
                for (const gid of ids) {
                    const list =
                        (s.getGuildScheduledEventsForGuild && s.getGuildScheduledEventsForGuild(gid)) ||
                        (s.getGuildEvents && s.getGuildEvents(gid)) || [];
                    raw.push(...list);
                }
            }
        } catch (e) {
            console.error("[Harvester] collect failed", e);
        }
        const seen = new Set();
        return raw.filter(ev => ev && ev.id && !seen.has(ev.id) && seen.add(ev.id));
    }

    _iso(v) {
        if (v == null) return null;
        try {
            // The store may hold an ISO string, an epoch, or a Moment-like
            // wrapper with toISOString(); new Date() handles the first two and
            // we reach through for the third.
            if (typeof v === "object" && typeof v.toISOString === "function")
                return v.toISOString();
            const d = new Date(v);
            return isNaN(d.getTime()) ? null : d.toISOString();
        } catch (e) { return null; }
    }

    _guildName(gid) {
        try {
            const g = this.guildStore && this.guildStore.getGuild && this.guildStore.getGuild(gid);
            return g ? g.name : "";
        } catch (e) { return ""; }
    }

    _build() {
        const events = this._collect().map(ev => ({
            id: String(ev.id),
            guild_id: String(ev.guildId || ev.guild_id || ""),
            guild_name: this._guildName(ev.guildId || ev.guild_id),
            name: ev.name || "",
            description: ev.description || "",
            scheduled_start_time: this._iso(ev.scheduledStartTime || ev.scheduled_start_time),
            scheduled_end_time: this._iso(ev.scheduledEndTime || ev.scheduled_end_time),
            // entity_type 3 == EXTERNAL, whose location lives in entityMetadata.
            location: (ev.entityMetadata && (ev.entityMetadata.location || ev.entityMetadata.location)) ||
                      (ev.entity_metadata && ev.entity_metadata.location) || "",
            entity_type: ev.entityType != null ? ev.entityType : ev.entity_type,
            status: ev.status,
            creator: (ev.creator && ev.creator.username) || "",
            // Cover flyer, if any. GSE `image` is a hash; the harvester
            // OCRs this URL for date/venue text that lives only in the art.
            image: ev.image
                ? `https://cdn.discordapp.com/guild-events/${ev.id}/${ev.image}.png?size=1024`
                : "",
        }));
        return { exported_at: new Date().toISOString(), source: "betterdiscord", events };
    }

    flush() {
        try {
            const payload = this._build();
            fs.mkdirSync(EXPORT_DIR, { recursive: true });
            fs.writeFileSync(EXPORT_FILE, JSON.stringify(payload, null, 1), "utf8");
            console.log("[Harvester] wrote " + payload.events.length + " scheduled event(s)");
        } catch (e) {
            console.error("[Harvester] write failed", e);
        }
    }
};
