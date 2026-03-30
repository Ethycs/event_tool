/**
 * Universal Controller — MV3 Content Script (world: "MAIN")
 *
 * Auto-detects UI patterns on any page and exposes results on window.__UC
 * so Playwright can read them via page.evaluate("window.__UC").
 *
 * Pattern detection ported from:
 *   https://github.com/Ethycs/universal_controller/blob/main/src/detection/patterns.js
 */

(function () {
  "use strict";

  // ── Pattern definitions (CSS selectors + scoring rules) ──────────────

  const PATTERNS = {
    search: {
      selectors: [
        '[role="search"]',
        'input[type="search"]',
        '[class*="search" i]',
        '[placeholder*="search" i]',
        '[aria-label*="search" i]',
        'input[name*="search" i]',
        'input[name*="query" i]',
        'input[name="q"]',
      ],
      keywords: ["search", "find", "explore", "discover", "look up", "query"],
    },
    feed: {
      selectors: [
        '[role="feed"]',
        '[class*="feed" i]',
        '[class*="timeline" i]',
        '[class*="posts" i]',
        '[class*="event-list" i]',
        '[class*="event-card" i]',
        '[class*="listing" i]',
        '[class*="results" i]',
      ],
      keywords: ["feed", "timeline", "posts", "events", "results"],
    },
    form: {
      selectors: [
        "form",
        '[role="form"]',
        '[class*="form" i]',
        "fieldset",
      ],
      keywords: ["submit", "register", "sign up", "apply"],
    },
    modal: {
      selectors: [
        '[role="dialog"]',
        '[aria-modal="true"]',
        '[class*="modal" i]',
        '[class*="dialog" i]',
        '[class*="popup" i]',
        '[class*="overlay" i]',
      ],
      keywords: ["close", "dismiss", "cancel"],
    },
    cookie_consent: {
      selectors: [
        '[class*="cookie" i]',
        '[class*="consent" i]',
        '[class*="gdpr" i]',
        '[id*="cookie" i]',
        '[id*="consent" i]',
        '[class*="banner" i]',
      ],
      keywords: [
        "accept",
        "agree",
        "cookie",
        "consent",
        "preferences",
        "got it",
        "i understand",
        "allow",
      ],
    },
    login_wall: {
      selectors: [
        '[class*="login" i]',
        '[class*="signin" i]',
        '[class*="sign-in" i]',
        '[class*="auth" i]',
      ],
      keywords: ["log in", "sign in", "password", "username", "email"],
    },
  };

  // ── Scoring helpers ──────────────────────────────────────────────────

  function isVisible(el) {
    if (!el || !el.getBoundingClientRect) return false;
    const rect = el.getBoundingClientRect();
    if (rect.width === 0 && rect.height === 0) return false;
    const style = getComputedStyle(el);
    return style.display !== "none" && style.visibility !== "hidden" && style.opacity !== "0";
  }

  function textScore(el, keywords) {
    const text = (
      (el.textContent || "") +
      " " +
      (el.getAttribute("placeholder") || "") +
      " " +
      (el.getAttribute("aria-label") || "") +
      " " +
      (el.getAttribute("title") || "")
    ).toLowerCase();
    let score = 0;
    for (const kw of keywords) {
      if (text.includes(kw)) score += 1;
    }
    return Math.min(score / keywords.length, 1.0);
  }

  function isScrollable(el) {
    const style = getComputedStyle(el);
    return (
      el.scrollHeight > el.clientHeight + 50 &&
      (style.overflowY === "auto" || style.overflowY === "scroll")
    );
  }

  function hasRepeatedChildren(el, minCount) {
    if (!el.children || el.children.length < minCount) return false;
    const tags = {};
    for (const child of el.children) {
      const key = child.tagName + "." + (child.className || "").split(" ").sort().join(".");
      tags[key] = (tags[key] || 0) + 1;
    }
    return Object.values(tags).some((c) => c >= minCount);
  }

  function uniqueSelector(el) {
    if (el.id) return "#" + CSS.escape(el.id);
    // Try aria-label
    const aria = el.getAttribute("aria-label");
    if (aria) return el.tagName.toLowerCase() + '[aria-label="' + aria.replace(/"/g, '\\"') + '"]';
    // Try name
    const name = el.getAttribute("name");
    if (name) return el.tagName.toLowerCase() + '[name="' + name.replace(/"/g, '\\"') + '"]';
    // Try type+placeholder for inputs
    if (el.tagName === "INPUT") {
      const ph = el.getAttribute("placeholder");
      if (ph) return 'input[placeholder="' + ph.replace(/"/g, '\\"') + '"]';
      const type = el.getAttribute("type");
      if (type) return "input[type=\"" + type + "\"]";
    }
    // Fall back to nth-child path
    const parts = [];
    let cur = el;
    while (cur && cur !== document.body && parts.length < 4) {
      let seg = cur.tagName.toLowerCase();
      if (cur.id) {
        parts.unshift("#" + CSS.escape(cur.id));
        break;
      }
      const parent = cur.parentElement;
      if (parent) {
        const siblings = Array.from(parent.children).filter((c) => c.tagName === cur.tagName);
        if (siblings.length > 1) {
          seg += ":nth-child(" + (Array.from(parent.children).indexOf(cur) + 1) + ")";
        }
      }
      parts.unshift(seg);
      cur = parent;
    }
    return parts.join(" > ");
  }

  // ── Pattern-specific detectors ───────────────────────────────────────

  function detectSearch() {
    const results = [];
    const candidates = new Set();
    for (const sel of PATTERNS.search.selectors) {
      for (const el of document.querySelectorAll(sel)) {
        candidates.add(el);
        // Also check inputs inside matched containers
        if (el.tagName !== "INPUT" && el.tagName !== "TEXTAREA") {
          for (const inp of el.querySelectorAll("input, textarea")) {
            candidates.add(inp);
          }
        }
      }
    }
    for (const el of candidates) {
      if (!isVisible(el)) continue;
      // Prefer actual input elements
      const isInput = el.tagName === "INPUT" || el.tagName === "TEXTAREA";
      let confidence = isInput ? 0.5 : 0.2;
      // Boost for type="search"
      if (el.getAttribute("type") === "search") confidence += 0.3;
      // Boost for role="search"
      if (el.getAttribute("role") === "search" || el.closest('[role="search"]')) confidence += 0.2;
      // Text/keyword boost
      confidence += textScore(el, PATTERNS.search.keywords) * 0.3;
      confidence = Math.min(confidence, 1.0);
      if (confidence >= 0.4 && isInput) {
        results.push({
          selector: uniqueSelector(el),
          confidence: Math.round(confidence * 100) / 100,
          placeholder: el.getAttribute("placeholder") || "",
          form_action: el.closest("form") ? el.closest("form").getAttribute("action") || "" : "",
          tag: el.tagName.toLowerCase(),
        });
      }
    }
    // Sort by confidence descending, dedupe
    results.sort((a, b) => b.confidence - a.confidence);
    return results.slice(0, 5);
  }

  function detectFeed() {
    const results = [];
    const candidates = new Set();
    for (const sel of PATTERNS.feed.selectors) {
      for (const el of document.querySelectorAll(sel)) {
        candidates.add(el);
      }
    }
    // Also scan for scrollable containers with repeated children
    for (const el of document.querySelectorAll("main, [role='main'], section, article, div")) {
      if (isScrollable(el) && hasRepeatedChildren(el, 3)) {
        candidates.add(el);
      }
    }
    for (const el of candidates) {
      if (!isVisible(el)) continue;
      let confidence = 0.3;
      if (isScrollable(el)) confidence += 0.25;
      if (hasRepeatedChildren(el, 3)) confidence += 0.25;
      if (hasRepeatedChildren(el, 8)) confidence += 0.1;
      confidence += textScore(el, PATTERNS.feed.keywords) * 0.2;
      confidence = Math.min(confidence, 1.0);
      if (confidence >= 0.5) {
        // Find the most common child pattern as item_selector
        let itemSelector = "";
        if (el.children.length > 0) {
          const tags = {};
          for (const child of el.children) {
            const key = child.tagName.toLowerCase() +
              (child.className ? "." + child.className.trim().split(/\s+/).join(".") : "");
            tags[key] = (tags[key] || 0) + 1;
          }
          const best = Object.entries(tags).sort((a, b) => b[1] - a[1])[0];
          if (best && best[1] >= 3) {
            // Use the tag + first class as selector
            const firstChild = Array.from(el.children).find(
              (c) => (c.tagName.toLowerCase() + (c.className ? "." + c.className.trim().split(/\s+/).join(".") : "")) === best[0]
            );
            if (firstChild) itemSelector = uniqueSelector(el) + " > " + firstChild.tagName.toLowerCase();
          }
        }
        results.push({
          selector: uniqueSelector(el),
          confidence: Math.round(confidence * 100) / 100,
          item_count: el.children.length,
          item_selector: itemSelector,
          scrollable: isScrollable(el),
        });
      }
    }
    results.sort((a, b) => b.confidence - a.confidence);
    return results.slice(0, 5);
  }

  function detectForm() {
    const results = [];
    for (const sel of PATTERNS.form.selectors) {
      for (const el of document.querySelectorAll(sel)) {
        if (!isVisible(el)) continue;
        const inputs = el.querySelectorAll("input, textarea, select");
        if (inputs.length === 0) continue;
        // Skip if it looks like a search form (single input)
        if (inputs.length === 1 && inputs[0].getAttribute("type") === "search") continue;
        let confidence = 0.3;
        if (el.tagName === "FORM") confidence += 0.3;
        if (inputs.length >= 2) confidence += 0.2;
        if (el.querySelector("button, [type='submit']")) confidence += 0.15;
        confidence += textScore(el, PATTERNS.form.keywords) * 0.15;
        confidence = Math.min(confidence, 1.0);
        if (confidence >= 0.5) {
          const fields = Array.from(inputs).slice(0, 10).map((inp) => ({
            name: inp.getAttribute("name") || "",
            type: inp.getAttribute("type") || inp.tagName.toLowerCase(),
            selector: uniqueSelector(inp),
          }));
          results.push({
            selector: uniqueSelector(el),
            confidence: Math.round(confidence * 100) / 100,
            fields,
          });
        }
      }
    }
    results.sort((a, b) => b.confidence - a.confidence);
    return results.slice(0, 5);
  }

  function detectModal() {
    const results = [];
    for (const sel of PATTERNS.modal.selectors) {
      for (const el of document.querySelectorAll(sel)) {
        if (!isVisible(el)) continue;
        let confidence = 0.3;
        const style = getComputedStyle(el);
        if (style.position === "fixed" || style.position === "absolute") confidence += 0.3;
        if (el.getAttribute("role") === "dialog") confidence += 0.25;
        if (el.getAttribute("aria-modal") === "true") confidence += 0.2;
        // Find close button
        const closeBtn =
          el.querySelector('[aria-label*="close" i], [class*="close" i], button:last-child') ||
          null;
        if (closeBtn) confidence += 0.1;
        confidence = Math.min(confidence, 1.0);
        if (confidence >= 0.5) {
          results.push({
            selector: uniqueSelector(el),
            confidence: Math.round(confidence * 100) / 100,
            dismissible: !!closeBtn,
            dismiss_selector: closeBtn ? uniqueSelector(closeBtn) : "",
          });
        }
      }
    }
    results.sort((a, b) => b.confidence - a.confidence);
    return results.slice(0, 3);
  }

  function detectCookieConsent() {
    const results = [];
    for (const sel of PATTERNS.cookie_consent.selectors) {
      for (const el of document.querySelectorAll(sel)) {
        if (!isVisible(el)) continue;
        const text = (el.textContent || "").toLowerCase();
        if (!text.includes("cookie") && !text.includes("consent") && !text.includes("gdpr") && !text.includes("privacy")) continue;
        let confidence = 0.4;
        const style = getComputedStyle(el);
        if (style.position === "fixed" || style.position === "sticky") confidence += 0.25;
        confidence += textScore(el, PATTERNS.cookie_consent.keywords) * 0.3;
        // Find the accept/agree button
        const acceptBtn =
          el.querySelector(
            'button[class*="accept" i], button[class*="agree" i], a[class*="accept" i]'
          ) ||
          Array.from(el.querySelectorAll("button, a")).find((b) => {
            const t = (b.textContent || "").toLowerCase().trim();
            return (
              t.includes("accept") ||
              t.includes("agree") ||
              t.includes("got it") ||
              t.includes("allow") ||
              t === "ok" ||
              t === "okay"
            );
          }) ||
          null;
        if (acceptBtn) confidence += 0.2;
        confidence = Math.min(confidence, 1.0);
        if (confidence >= 0.5) {
          results.push({
            selector: uniqueSelector(el),
            confidence: Math.round(confidence * 100) / 100,
            action: "click",
            accept_selector: acceptBtn ? uniqueSelector(acceptBtn) : "",
          });
        }
      }
    }
    results.sort((a, b) => b.confidence - a.confidence);
    return results.slice(0, 2);
  }

  function detectLoginWall() {
    const results = [];
    for (const sel of PATTERNS.login_wall.selectors) {
      for (const el of document.querySelectorAll(sel)) {
        if (!isVisible(el)) continue;
        const hasPassword = !!el.querySelector('input[type="password"]');
        if (!hasPassword) continue;
        let confidence = 0.4;
        if (hasPassword) confidence += 0.3;
        const style = getComputedStyle(el);
        const blocking =
          style.position === "fixed" ||
          style.position === "absolute" ||
          el.closest('[role="dialog"]') !== null;
        if (blocking) confidence += 0.2;
        confidence += textScore(el, PATTERNS.login_wall.keywords) * 0.2;
        confidence = Math.min(confidence, 1.0);
        if (confidence >= 0.5) {
          results.push({
            selector: uniqueSelector(el),
            confidence: Math.round(confidence * 100) / 100,
            blocking,
          });
        }
      }
    }
    results.sort((a, b) => b.confidence - a.confidence);
    return results.slice(0, 2);
  }

  // ── Main detection + API exposure ────────────────────────────────────

  function runDetection() {
    return {
      search: detectSearch(),
      feed: detectFeed(),
      form: detectForm(),
      modal: detectModal(),
      cookie_consent: detectCookieConsent(),
      login_wall: detectLoginWall(),
    };
  }

  function init() {
    window.__UC = {
      version: "0.1.0",
      ready: false,
      timestamp: 0,
      url: location.href,
      patterns: {},
    };

    try {
      window.__UC.patterns = runDetection();
    } catch (e) {
      console.warn("[UC] Detection error:", e);
      window.__UC.patterns = {};
    }

    window.__UC.timestamp = Date.now();
    window.__UC.ready = true;
  }

  // ── Exposed actions ──────────────────────────────────────────────────

  window.__UC_rescan = function () {
    init();
    return window.__UC;
  };

  window.__UC_dismiss = function () {
    const uc = window.__UC;
    if (!uc || !uc.patterns || !uc.patterns.cookie_consent) return false;
    for (const cc of uc.patterns.cookie_consent) {
      if (cc.accept_selector) {
        const btn = document.querySelector(cc.accept_selector);
        if (btn) {
          btn.click();
          return true;
        }
      }
    }
    return false;
  };

  window.__UC_fillSearch = function (query) {
    const uc = window.__UC;
    if (!uc || !uc.patterns || !uc.patterns.search || uc.patterns.search.length === 0) return false;
    const best = uc.patterns.search[0];
    const el = document.querySelector(best.selector);
    if (!el) return false;
    // Framework-agnostic input: set value + dispatch events
    const nativeSetter = Object.getOwnPropertyDescriptor(
      window.HTMLInputElement.prototype, "value"
    ).set;
    nativeSetter.call(el, query);
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    return true;
  };

  window.__UC_getVisibleText = function () {
    const uc = window.__UC;
    if (!uc || !uc.patterns || !uc.patterns.feed || uc.patterns.feed.length === 0) {
      return document.body.innerText;
    }
    const best = uc.patterns.feed[0];
    const el = document.querySelector(best.selector);
    return el ? el.innerText : document.body.innerText;
  };

  // Run on load
  init();
})();
