/**
 * @name HarvesterMessageCapture
 * @author eventharvester
 * @description Passively records the Discord messages YOU load by browsing — as you open channels and scroll, it writes what your client already fetched to local JSONL files for the event harvester. It never fetches, scrolls, or requests anything itself: it only observes and records normal human browsing.
 * @version 1.0.0
 */

// ── Why this is the safe shape ──────────────────────────────────────────────
//
// Bulk history export via the user token is self-botting — the account makes
// thousands of automated requests, which is what Discord bans for. This does
// the opposite: it subscribes to the Flux events Discord fires when the client
// loads messages *because you opened a channel or scrolled*, and records what
// was already fetched. No request originates here. To capture three months of a
// channel you open it and scroll up until you reach that date — that is you
// reading Discord, which is unambiguously fine. This plugin adds zero traffic.
//
// Output: %APPDATA%\discord-harvester\messages\<guildId>_<channelId>.jsonl
// (Windows) or ~/.config/discord-harvester/messages/... — one line per message,
// appended as you browse. Duplicates are fine: the harvester dedups by id on
// read. There is deliberately NO auto-scroll and NO fetch — adding either would
// turn this back into the thing it exists to avoid.

const fs = require("fs");
const path = require("path");
// No require("os"): BetterDiscord's addon loader resolves that bare name as a
// file path and the plugin fails to load (ENOENT …/os).

const BASE = path.join(
    process.env.APPDATA ||
        path.join(process.env.HOME || process.env.USERPROFILE || "", ".config"),
    "discord-harvester", "messages"
);

// Flux events that carry messages the client fetched on your behalf.
//   LOAD_MESSAGES_SUCCESS — a history chunk arrived (channel open / scroll-up)
//   MESSAGE_CREATE        — a new live message
//   MESSAGE_UPDATE        — an edit
const WATCH = ["LOAD_MESSAGES_SUCCESS", "MESSAGE_CREATE", "MESSAGE_UPDATE"];

module.exports = class HarvesterMessageCapture {
    start() {
        this._subs = [];
        this._seen = new Set();          // in-memory dedup for this session
        this._buffers = new Map();       // channelId -> [lines] pending flush
        this.channelStore = BdApi.Webpack.getStore("ChannelStore");
        this.guildStore = BdApi.Webpack.getStore("GuildStore");
        this.dispatcher = BdApi.Webpack.getByKeys("dispatch", "subscribe");

        if (!this.dispatcher) {
            BdApi.UI.showToast("Harvester: Flux dispatcher not found", { type: "error" });
            return;
        }

        this._onLoad = (a) => this._onMessages(a);
        this._onCreate = (a) => this._record(a && a.message);
        this._onUpdate = (a) => this._record(a && a.message);
        try { this.dispatcher.subscribe("LOAD_MESSAGES_SUCCESS", this._onLoad); this._subs.push(["LOAD_MESSAGES_SUCCESS", this._onLoad]); } catch (e) {}
        try { this.dispatcher.subscribe("MESSAGE_CREATE", this._onCreate); this._subs.push(["MESSAGE_CREATE", this._onCreate]); } catch (e) {}
        try { this.dispatcher.subscribe("MESSAGE_UPDATE", this._onUpdate); this._subs.push(["MESSAGE_UPDATE", this._onUpdate]); } catch (e) {}

        this._timer = setInterval(() => this._flush(), 5000);
        try { fs.mkdirSync(BASE, { recursive: true }); } catch (e) {}
        console.log("[Harvester] message capture -> " + BASE);
        BdApi.UI.showToast("Harvester: capturing messages you browse", { type: "info" });
    }

    stop() {
        if (this._timer) clearInterval(this._timer);
        for (const [ev, cb] of (this._subs || [])) {
            try { this.dispatcher.unsubscribe(ev, cb); } catch (e) {}
        }
        this._flush();                   // don't lose the last few seconds
    }

    _onMessages(action) {
        const list = action && action.messages;
        if (Array.isArray(list)) for (const m of list) this._record(m);
    }

    _record(m) {
        if (!m || !m.id || !m.channel_id) return;
        if (this._seen.has(m.id)) return;
        this._seen.add(m.id);

        const ch = this.channelStore && this.channelStore.getChannel(m.channel_id);
        const guildId = (ch && ch.guild_id) || (m.guild_id) || "dm";
        const guild = guildId !== "dm" && this.guildStore
            ? (this.guildStore.getGuild(guildId) || {}).name : "Direct Messages";

        const rec = {
            id: String(m.id),
            channel_id: String(m.channel_id),
            channel_name: (ch && (ch.name || ch.rawRecipients && "DM")) || "",
            guild_id: String(guildId),
            guild_name: guild || "",
            author: (m.author && (m.author.global_name || m.author.username)) || "",
            author_id: (m.author && String(m.author.id)) || "",
            timestamp: m.timestamp || "",
            edited_timestamp: m.edited_timestamp || null,
            content: m.content || "",
            attachments: (m.attachments || []).map(a => a.url).filter(Boolean),
            embeds: (m.embeds || []).length,
        };

        const key = `${guildId}_${m.channel_id}`;
        if (!this._buffers.has(key)) this._buffers.set(key, []);
        this._buffers.get(key).push(JSON.stringify(rec));

        // Cap session dedup memory so a marathon browse can't grow unbounded.
        if (this._seen.size > 500000) this._seen.clear();
    }

    _flush() {
        if (!this._buffers.size) return;
        for (const [key, lines] of this._buffers) {
            if (!lines.length) continue;
            try {
                fs.mkdirSync(BASE, { recursive: true });
                fs.appendFileSync(path.join(BASE, key + ".jsonl"), lines.join("\n") + "\n", "utf8");
            } catch (e) {
                console.error("[Harvester] flush failed for " + key, e);
                continue;
            }
            lines.length = 0;
        }
    }
};
