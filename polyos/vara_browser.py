"""Vara's web browser: a real browser Vara drives with Playwright, in a window of its own you can watch.

Vara sees a page the way a person skims it: the title, the address, the readable text, and a
numbered list of what can be clicked or filled in ("[3] button “Sign in”", "[4] textbox “Email”").
It acts by number: click 3, type into 4, pick from a list, press a key, scroll, go back, switch tabs,
and takes screenshots. This is the approach of open-source browser agents (Playwright MCP,
browser-use), which works with any chat model because the page is text.

It runs as its own process (python -m polyos.vara_browser, with the Python in Vara Voice's
folder, which has Playwright) using Debian's Chromium and its own profile in
~/.local/share/polyos/vara/browser, so your own browser, its logins and history are never touched.
The tool side (vara_tools.web) starts it on first use and closes it after ten idle minutes.
Commands are JSON lines on stdin; each answer is a JSON line on stdout.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path

PROFILE = Path.home() / ".local" / "share" / "polyos" / "vara" / "browser"
SHOTS = Path.home() / "Pictures" / "Vara"
MAX_TEXT = 6000
MAX_ELEMENTS = 150

# Marks each visible thing you can use with a number (data-vara-ref) and lists them.
SNAPSHOT_JS = r"""
(max) => {
  const out = [];
  const seen = new Set();
  const sel = 'a[href], button, input:not([type=hidden]), textarea, select, summary, [role=button], [role=link], ' +
    '[role=tab], [role=menuitem], [role=checkbox], [role=radio], [role=switch], [role=option], [role=combobox], [contenteditable=""], [contenteditable=true]';
  document.querySelectorAll('[data-vara-ref]').forEach((el) => el.removeAttribute('data-vara-ref'));
  let n = 0;
  for (const el of document.querySelectorAll(sel)) {
    if (out.length >= max) break;
    const r = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    if (r.width < 2 || r.height < 2 || style.visibility === 'hidden' || style.display === 'none' || el.disabled) continue;
    if (seen.has(el)) continue;
    seen.add(el);
    n += 1;
    el.setAttribute('data-vara-ref', String(n));
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || '').toLowerCase();
    const role = el.getAttribute('role') || (tag === 'a' ? 'link' : tag === 'select' ? 'list' : tag === 'textarea' ? 'textbox'
      : tag === 'input' ? (['checkbox', 'radio', 'submit', 'button'].includes(type) ? type : 'textbox') : tag);
    // named the way a person sees it: its aria-label, its <label>, its own text, then hints
    const byId = el.getAttribute('aria-labelledby') && document.getElementById(el.getAttribute('aria-labelledby'));
    const labelText = el.labels && el.labels.length ? [...el.labels].map((l) => l.innerText).join(' ') : '';
    const isButton = tag === 'button' || ['submit', 'button', 'reset'].includes(type);
    const label = (el.getAttribute('aria-label') || (byId ? byId.innerText : '') || labelText || el.innerText ||
      el.getAttribute('placeholder') || (isButton ? el.value : '') || el.getAttribute('title') || el.getAttribute('alt') ||
      el.getAttribute('name') || '').trim().replace(/\s+/g, ' ').slice(0, 80);
    let extra = '';
    if (role === 'textbox' && el.value && type !== 'password') extra = ` value="${String(el.value).slice(0, 40)}"`;
    if (type === 'password') extra = ' (password)';
    if (['checkbox', 'radio'].includes(role)) extra = el.checked ? ' (checked)' : '';
    if (tag === 'select') extra = ` options: ${[...el.options].slice(0, 12).map((o) => o.text.trim()).join(' | ')}`;
    if (tag === 'a') extra = ` → ${el.getAttribute('href').slice(0, 80)}`;
    const place = r.top > innerHeight ? ' (below)' : r.bottom < 0 ? ' (above)' : '';
    out.push(`[${n}] ${role} “${label}”${extra}${place}`);
  }
  const main = document.querySelector('main, article, [role=main]') || document.body;
  const text = (main ? main.innerText : '').replace(/\n{3,}/g, '\n\n').trim();
  return { title: document.title, url: location.href, text, elements: out,
           scroll: Math.round(100 * (scrollY + innerHeight) / Math.max(1, document.documentElement.scrollHeight)) };
}
"""


def chromium_path() -> str | None:
    for candidate in (os.environ.get("POLYOS_BROWSER"), shutil.which("chromium"), shutil.which("chromium-browser"),
                      shutil.which("google-chrome")):
        if candidate and Path(candidate).exists():
            return candidate
    return None  # Playwright's own Chromium, if installed


class Browser:
    def __init__(self, headless: bool | None = None):
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        PROFILE.mkdir(parents=True, exist_ok=True)
        headless = (not os.environ.get("DISPLAY")) if headless is None else headless
        self.context = self._pw.chromium.launch_persistent_context(
            str(PROFILE), headless=headless, executable_path=chromium_path(), viewport={"width": 1280, "height": 860},
            args=["--no-first-run", "--no-default-browser-check", "--disable-features=Translate"])
        self.context.set_default_timeout(15000)
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()

    # the page as text, with numbered things to use
    def snapshot(self, text_chars: int = MAX_TEXT) -> str:
        try:
            self.page.wait_for_load_state("domcontentloaded", timeout=8000)
        except Exception:  # noqa: BLE001 - a slow page still gets described
            pass
        snap = self.page.evaluate(SNAPSHOT_JS, MAX_ELEMENTS)
        text = snap["text"]
        if len(text) > text_chars:
            text = text[:text_chars] + f"\n… (more: use extract, or scroll; {len(snap['text'])} characters in all)"
        tabs = len(self.context.pages)
        return (f"Page: {snap['title']}\nAddress: {snap['url']}\nScrolled: {snap['scroll']}%" + (f" · {tabs} tabs" if tabs > 1 else "")
                + f"\n\n{text}\n\nThings to use (act by number):\n" + ("\n".join(snap["elements"]) or "(none)"))

    def _ref(self, ref):
        loc = self.page.locator(f'[data-vara-ref="{int(ref)}"]')
        if loc.count() == 0:
            raise ValueError(f"There's no [{ref}] on the page now. Take a snapshot to see the current numbers.")
        return loc.first

    def _settle(self) -> None:
        try:
            self.page.wait_for_load_state("domcontentloaded", timeout=10000)
            self.page.wait_for_timeout(400)
        except Exception:  # noqa: BLE001
            pass

    def run(self, cmd: dict) -> str:
        action = cmd.get("action")
        if action == "open":
            url = str(cmd.get("url") or "")
            if not url.startswith(("http://", "https://")):
                url = "https://" + url
            self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
            self._settle()
            return self.snapshot()
        if action == "search":
            from urllib.parse import quote_plus
            self.page.goto("https://duckduckgo.com/?q=" + quote_plus(str(cmd.get("query") or "")), wait_until="domcontentloaded")
            self._settle()
            return self.snapshot()
        if action == "snapshot":
            return self.snapshot()
        if action == "click":
            before = len(self.context.pages)
            self._ref(cmd.get("ref")).click()
            self._settle()
            if len(self.context.pages) > before:  # the link opened a new tab: follow it
                self.page = self.context.pages[-1]
                self.page.bring_to_front()
                self._settle()
            return self.snapshot()
        if action == "type":
            target = self._ref(cmd.get("ref"))
            target.fill(str(cmd.get("text") or ""))
            if cmd.get("submit"):
                target.press("Enter")
                self._settle()
            return self.snapshot()
        if action == "select":
            self._ref(cmd.get("ref")).select_option(label=str(cmd.get("option") or ""))
            self._settle()
            return self.snapshot()
        if action == "check":
            target = self._ref(cmd.get("ref"))
            target.check() if cmd.get("on", True) else target.uncheck()
            return self.snapshot()
        if action == "press":
            self.page.keyboard.press(str(cmd.get("key") or "Enter"))
            self._settle()
            return self.snapshot()
        if action == "scroll":
            self.page.mouse.wheel(0, -800 if cmd.get("direction") == "up" else 800)
            self.page.wait_for_timeout(350)
            return self.snapshot()
        if action in ("back", "forward", "reload"):
            getattr(self.page, {"back": "go_back", "forward": "go_forward", "reload": "reload"}[action])()
            self._settle()
            return self.snapshot()
        if action == "extract":
            return self.snapshot(text_chars=20000)
        if action == "tabs":
            return "\n".join(f"[{i}] {p.title() or p.url}{' (this one)' if p == self.page else ''}" for i, p in enumerate(self.context.pages))
        if action == "tab":
            pages = self.context.pages
            index = int(cmd.get("index", 0))
            if not 0 <= index < len(pages):
                raise ValueError("There's no tab with that number.")
            self.page = pages[index]
            self.page.bring_to_front()
            return self.snapshot()
        if action == "new_tab":
            self.page = self.context.new_page()
            if cmd.get("url"):
                return self.run({"action": "open", "url": cmd["url"]})
            return "Opened a new tab."
        if action == "close_tab":
            self.page.close()
            if not self.context.pages:
                self.context.new_page()
            self.page = self.context.pages[-1]
            return self.snapshot()
        if action == "screenshot":
            SHOTS.mkdir(parents=True, exist_ok=True)
            path = SHOTS / f"page-{time.strftime('%Y%m%d-%H%M%S')}.png"
            self.page.screenshot(path=str(path), full_page=bool(cmd.get("full_page")))
            return f"Saved a screenshot: {path}"
        if action == "wait":
            self.page.wait_for_timeout(min(15, float(cmd.get("seconds") or 2)) * 1000)
            return self.snapshot()
        raise ValueError(f"Unknown browser action: {action}")

    def close(self) -> None:
        try:
            self.context.close()
        finally:
            self._pw.stop()


def main() -> int:
    browser = None
    for line in sys.stdin:
        try:
            cmd = json.loads(line)
        except ValueError:
            continue
        reply = {"id": cmd.get("id")}
        try:
            if cmd.get("action") == "quit":
                break
            if browser is None:
                browser = Browser(headless=cmd.get("headless"))
            reply["result"] = browser.run(cmd)
        except Exception as exc:  # noqa: BLE001 - every problem goes back to Vara as text
            message = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
            reply["error"] = message[:500]
        sys.stdout.write(json.dumps(reply) + "\n")
        sys.stdout.flush()
    if browser is not None:
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
