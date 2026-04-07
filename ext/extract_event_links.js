/**
 * DOM-graph event link extraction.
 *
 * Walks the DOM tree to associate dates with links via locality:
 *   Step 1: Find text nodes containing dates
 *   Step 2: Walk up to find the containing "card" (repeated-sibling ancestor)
 *   Step 3: Find the primary link in each card
 *   Step 4: Fallback — repeated-child containers
 *   Step 4b: JavaScript links with embedded event data
 *   Step 5: Last resort — links near dates (walk up 5 ancestors)
 *
 * Returns: [{url, text, date_hint, time?, venue?, inline?}]
 */
() => {
    const DATE_RE = /(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2}(?:st|nd|rd|th)?|\d{1,2}(?:st|nd|rd|th)?\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*|\d{4}-\d{2}-\d{2}|(?:mon|tue|wed|thu|fri|sat|sun)(?:day)?(?:\s*,)?\s*(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2}/i;
    const TIME_RE = /\b\d{1,2}(?::\d{2})?\s*(?:am|pm)\b|\b(?:[01]\d|2[0-3]):[0-5]\d\b/i;
    const domain = location.hostname;

    // ── Step 1: Walk the DOM tree to find all text nodes with dates ──
    const dateNodes = [];
    const walker = document.createTreeWalker(
        document.body, NodeFilter.SHOW_TEXT, null
    );
    while (walker.nextNode()) {
        const text = walker.currentNode.textContent.trim();
        if (text.length > 3 && text.length < 200 && DATE_RE.test(text)) {
            dateNodes.push({
                node: walker.currentNode,
                text: text,
                dateMatch: text.match(DATE_RE)?.[0] || '',
                hasTime: TIME_RE.test(text),
            });
        }
    }

    // ── Step 2: For each date node, walk up to find its "card" ──────
    // A card is the nearest ancestor that has siblings of the same type
    // (indicating it's an item in a repeated list/grid)
    function findCard(node) {
        let cur = node.parentElement;
        let bestCard = null;
        while (cur && cur !== document.body) {
            const parent = cur.parentElement;
            if (parent) {
                const cn = typeof cur.className === 'string' ? cur.className : '';
                const tag = cur.tagName + '.' + cn.split(' ')[0];
                let sibCount = 0;
                for (const sib of parent.children) {
                    const scn = typeof sib.className === 'string' ? sib.className : '';
                    if (sib.tagName + '.' + scn.split(' ')[0] === tag) sibCount++;
                }
                if (sibCount >= 2) {
                    bestCard = cur;
                    // Keep walking if this card has no links (too narrow)
                    if (cur.querySelectorAll('a[href]').length > 0) return cur;
                }
            }
            cur = cur.parentElement;
        }
        return bestCard;
    }

    // ── Step 3: From each card, find the primary link ───────────────
    const results = [];
    const seenUrls = new Set();
    const seenCards = new Set();

    for (const dn of dateNodes) {
        const card = findCard(dn.node);
        if (!card || seenCards.has(card)) continue;
        seenCards.add(card);

        // Find the best link in this card (longest text = likely title)
        const links = card.querySelectorAll('a[href]');
        let bestLink = null;
        let bestLen = 0;
        for (const a of links) {
            const href = a.href;
            if (!href || seenUrls.has(href)) continue;
            try { if (new URL(href).hostname !== domain) continue; } catch { continue; }
            const linkText = a.innerText.trim();
            if (linkText.length > bestLen) {
                bestLink = a;
                bestLen = linkText.length;
            }
        }

        // Accept link with text, or image-wrapped link (empty text but long slug)
        if (bestLink && bestLen >= 5) {
            seenUrls.add(bestLink.href);
            results.push({
                url: bestLink.href,
                text: bestLink.innerText.trim().substring(0, 200) || card.innerText.trim().substring(0, 200),
                date_hint: dn.dateMatch,
            });
        } else if (bestLink && bestLen < 5) {
            // Image-wrapped link — use card text as the title
            const cardTitle = card.innerText.trim().split('\n').filter(l => l.length > 5)[0] || '';
            if (cardTitle.length >= 5) {
                seenUrls.add(bestLink.href);
                results.push({
                    url: bestLink.href,
                    text: cardTitle.substring(0, 200),
                    date_hint: dn.dateMatch,
                });
            }
        }
    }

    // ── Step 4: Fallback — if few date associations, try repeated ───
    // containers and extract primary links from each child item
    if (results.length < 3) {
        const containers = [];
        document.querySelectorAll('main, [role="main"], section, div, ul, ol, table, tbody').forEach(el => {
            if (el.children.length < 3) return;
            const tags = {};
            for (const c of el.children) {
                const cn = typeof c.className === 'string' ? c.className : '';
                tags[c.tagName + '.' + cn.split(' ')[0]] = (tags[c.tagName + '.' + cn.split(' ')[0]] || 0) + 1;
            }
            const max = Math.max(...Object.values(tags));
            if (max >= 3) containers.push({ el, count: max });
        });
        containers.sort((a, b) => b.count - a.count);

        // Use top container, skip if it's a parent of an already-used one
        for (const { el: cont } of containers.slice(0, 2)) {
            for (const item of cont.children) {
                const links = item.querySelectorAll('a[href]');
                let best = null, bestLen = 0;
                for (const a of links) {
                    if (!a.href || seenUrls.has(a.href)) continue;
                    try { if (new URL(a.href).hostname !== domain) continue; } catch { continue; }
                    const t = a.innerText.trim();
                    if (t.length > bestLen) { best = a; bestLen = t.length; }
                }
                if (best && bestLen >= 5) {
                    seenUrls.add(best.href);
                    // Check if this item has a date too
                    const itemText = item.innerText || '';
                    const dateMatch = itemText.match(DATE_RE);
                    results.push({
                        url: best.href,
                        text: best.innerText.trim().substring(0, 200),
                        date_hint: dateMatch ? dateMatch[0] : '',
                    });
                }
            }
        }
    }

    // ── Step 4b: JavaScript links with embedded data ────────────────
    // Some calendars use javascript: hrefs that encode the event name,
    // date, and ID directly. Extract time and venue from DOM context.
    if (results.length < 3) {
        const jsLinks = document.querySelectorAll('a[href^="JavaScript:" i], a[href^="javascript:" i]');
        const jsDateRe = /(\d{4})\/+(\d{1,2})\/+(\d{1,2})/;
        for (const a of jsLinks) {
            const text = a.innerText.trim();
            if (text.length < 5) continue;
            const href = a.getAttribute('href') || '';
            const dm = href.match(jsDateRe);
            if (!dm) continue;
            const dateStr = dm[1] + '-' + dm[2].padStart(2, '0') + '-' + dm[3].padStart(2, '0');
            if (seenUrls.has(text + dateStr)) continue;
            seenUrls.add(text + dateStr);

            // Extract time from sibling TimeLabel
            const container = a.closest('.CalEvent') || a.closest('div') || a.parentElement;
            const timeEl = container ? container.querySelector('.TimeLabel') : null;
            const time = timeEl ? timeEl.innerText.trim() : '';

            // Extract venue from parent table class (e.g. c_FilthyStudios)
            const venueTable = a.closest('table[class]');
            let venue = '';
            if (venueTable) {
                venue = venueTable.className
                    .replace(/^c_/, '')
                    .replace(/([A-Z])/g, ' $1')
                    .trim();
            }

            // Build event detail URL from PopupWindow params
            const idMatch = href.match(/['"]\s*,\s*['"]\d{4}\/\d+\/\d+['"]\s*,\s*['"](\d+)/);
            const calMatch = href.match(/PopupWindow\s*\(\s*['"]([^'"]+)/);
            let eventUrl = location.origin + location.pathname;
            if (idMatch && calMatch) {
                const calName = calMatch[1];
                const eventId = idMatch[1];
                eventUrl = location.origin + '/calendar/Calcium40.pl?CalendarName='
                    + encodeURIComponent(calName)
                    + '&Op=PopupWindow&Date=' + encodeURIComponent(dm[0])
                    + '&ID=' + eventId;
            }

            results.push({
                url: eventUrl,
                text: text.substring(0, 200),
                date_hint: dateStr,
                time: time,
                venue: venue,
                // Keep time/venue as hints but always fetch detail pages for full descriptions
            });
        }
    }

    // ── Step 5: Last resort — links near dates (walk up 5 ancestors) ──
    if (results.length < 3) {
        const allLinks = document.querySelectorAll('a[href]');
        for (const a of allLinks) {
            if (seenUrls.has(a.href)) continue;
            const text = a.innerText.trim();
            if (text.length < 5) continue;
            try { if (new URL(a.href).hostname !== domain) continue; } catch { continue; }
            if (/^(home|about|contact|login|sign|help|privacy|terms|\d{1,2})$/i.test(text)) continue;

            // Walk up 5 ancestors looking for date context
            let dateMatch = text.match(DATE_RE)?.[0] || '';
            if (!dateMatch) {
                let cur = a.parentElement;
                for (let depth = 0; cur && depth < 5; depth++, cur = cur.parentElement) {
                    const ct = (cur.childNodes.length <= 5)
                        ? (cur.innerText || '').substring(0, 300)
                        : '';
                    const m = ct.match(DATE_RE);
                    if (m) { dateMatch = m[0]; break; }
                }
            }

            // Accept if date found nearby, or if the URL pattern suggests event detail
            const urlPath = new URL(a.href).pathname;
            const looksLikeDetail = /\/\w{5,}.*\d/.test(urlPath) && urlPath.split('/').length >= 3;
            if (!dateMatch && !looksLikeDetail) continue;

            seenUrls.add(a.href);
            results.push({
                url: a.href,
                text: text.substring(0, 200),
                date_hint: dateMatch,
            });
        }
    }

    return results;
}
