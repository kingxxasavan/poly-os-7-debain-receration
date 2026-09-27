#!/usr/bin/env python3
"""Writes the website's secondary pages (docs/*.html) around one shared header and footer.

    python3 tools/website_pages.py

index.html and install.html are written by hand but use the same header and footer (this script
replaces the <header class="nav"> and <footer> blocks in them too). The pages are plain static
HTML, as Vercel serves docs/ as it is.
"""

from __future__ import annotations

import re
from html import escape
from pathlib import Path

DOCS = Path(__file__).resolve().parent.parent / "docs"

ICONS = {
    "download": '<path d="M12 4v11M7 10.5l5 5 5-5M5 19.5h14"/>',
    "user": '<circle cx="12" cy="8.5" r="3.8"/><path d="M4.5 20c.9-3.8 3.9-5.8 7.5-5.8s6.6 2 7.5 5.8"/>',
    "grid": '<rect x="4" y="4" width="6.5" height="6.5" rx="1.8"/><rect x="13.5" y="4" width="6.5" height="6.5" rx="1.8"/><rect x="4" y="13.5" width="6.5" height="6.5" rx="1.8"/><rect x="13.5" y="13.5" width="6.5" height="6.5" rx="1.8"/>',
    "desktop": '<rect x="3" y="4.5" width="18" height="12" rx="2.5"/><path d="M9 20h6M12 16.5V20"/>',
    "laptop": '<rect x="5" y="5" width="14" height="10" rx="1.5"/><path d="M2.5 18.5h19"/>',
    "sync": '<path d="M19.5 11a7.5 7.5 0 0 0-13.4-4.3L4.5 8.5M4.5 13a7.5 7.5 0 0 0 13.4 4.3l1.6-1.8"/><path d="M4.5 4v4.5H9M19.5 20v-4.5H15"/>',
    "shield": '<path d="M12 3.5 19 6v5.5c0 4.3-2.9 7.6-7 9-4.1-1.4-7-4.7-7-9V6z"/><path d="m9 12 2.2 2.2L15.5 10"/>',
    "eye": '<path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12z"/><circle cx="12" cy="12" r="3"/>',
    "key": '<circle cx="8" cy="14" r="4"/><path d="m11 11 8.5-8.5M16 6l2.5 2.5M14 8l2 2"/>',
    "bell": '<path d="M6 16.5V11a6 6 0 0 1 12 0v5.5l1.5 2h-15z"/><path d="M10 20.5a2 2 0 0 0 4 0"/>',
    "help": '<circle cx="12" cy="12" r="8.5"/><path d="M9.6 9.5a2.5 2.5 0 1 1 3.5 2.3c-.7.3-1.1.9-1.1 1.7v.5M12 17v.2"/>',
    "book": '<path d="M5 4.5h10.5a3 3 0 0 1 3 3V20H8a3 3 0 0 1-3-3z"/><path d="M5 17a3 3 0 0 1 3-3h10.5"/>',
    "logout": '<path d="M14 4.5h4a2 2 0 0 1 2 2v11a2 2 0 0 1-2 2h-4M10 16.5 5.5 12 10 7.5M5.5 12H16"/>',
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "check": '<path d="m5 12.5 4.5 4.5L19 7.5"/>',
    "info": '<circle cx="12" cy="12" r="8.5"/><path d="M12 11v5.5M12 7.8v.2"/>',
    "lock": '<rect x="5" y="10.5" width="14" height="10" rx="2.5"/><path d="M8 10.5V8a4 4 0 0 1 8 0v2.5"/>',
    "arrow": '<path d="M5 12h14M13 6l6 6-6 6"/>',
    "close": '<path d="m6 6 12 12M18 6 6 18"/>',
    "mail": '<rect x="3.5" y="5.5" width="17" height="13" rx="2.5"/><path d="m4.5 7 7.5 6 7.5-6"/>',
}


def sprite(names) -> str:
    return ('<svg width="0" height="0" style="position:absolute" aria-hidden="true"><defs>'
            + "".join(f'<symbol id="i-{n}" viewBox="0 0 24 24">{ICONS[n]}</symbol>' for n in names)
            + "</defs></svg>")


def ico(name: str) -> str:
    return f'<svg class="ico"><use href="#i-{name}"/></svg>'


NAV = [("/download", "Download"), ("/#features", "Features"), ("/docs", "Docs"), ("/changelog", "Updates"),
       ("/security", "Security"), ("/support", "Support")]

HEADER = (
    '<header class="nav" id="top">\n'
    '    <a class="brand" href="/"><img src="/assets/logo.svg" alt="" width="30" height="30"><span>PolyOS <b>7</b></span></a>\n'
    '    <nav aria-label="Site">\n'
    + "".join(f'      <a href="{href}">{label}</a>\n' for href, label in NAV)
    + '    </nav>\n'
    f'    <a class="btn small" href="/account">{ico("user")}Poly Account</a>\n'
    '  </header>'
)

FOOTER = (
    '<footer class="footer site-foot">\n'
    '    <div class="foot-brand"><img src="/assets/logo.svg" alt="" width="22" height="22"><span><b>PolyOS 7</b> for Debian · by Cryptic Software</span></div>\n'
    '    <nav aria-label="More">\n'
    '      <a href="/download">Download</a><a href="/install">Install guide</a><a href="/docs">Documentation</a><a href="/changelog">Updates</a>\n'
    '      <a href="/support">Help &amp; Support</a><a href="/security">Security</a><a href="/privacy">Privacy Policy</a><a href="/terms">Terms of Service</a>\n'
    '      <a href="/account">Poly Account</a>\n'
    '    </nav>\n'
    '    <p class="foot-note">PolyOS works without an online account. The PolyOS logo and artwork come from the Scratch project by PIXAPoLY Software (CC BY-SA 2.0).</p>\n'
    '  </footer>'
)


def page(slug: str, title: str, description: str, body: str, *, icons=(), scripts=(), body_attrs: str = "", main_class: str = "page-main") -> None:
    names = sorted({"user", "download", *icons})
    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)}</title>
  <meta name="description" content="{escape(description)}">
  <meta name="theme-color" content="#151515">
  <link rel="icon" href="/assets/logo.svg" type="image/svg+xml">
  <link rel="stylesheet" href="/assets/site.css">
{"".join(f'  <script src="{s}" defer></script>{chr(10)}' for s in scripts)}</head>
<body{body_attrs}>
  {sprite(names)}

  {HEADER}

  <main class="{main_class}">
{body}
  </main>

  {FOOTER}
</body>
</html>
"""
    (DOCS / f"{slug}.html").write_text(html, "utf-8")
    print(f"  wrote docs/{slug}.html")


def prose(eyebrow: str, title: str, intro: str, sections: list[tuple[str, str]], updated: str | None = None) -> str:
    toc = "".join(f'<li><a href="#{re.sub(r"[^a-z0-9]+", "-", h.lower()).strip("-")}">{escape(h)}</a></li>' for h, _ in sections)
    parts = "".join(f'\n    <section id="{re.sub(r"[^a-z0-9]+", "-", h.lower()).strip("-")}"><h2>{escape(h)}</h2>{b}</section>' for h, b in sections)
    stamp = f'<p class="doc-updated">{escape(updated)}</p>' if updated else ""
    return f"""    <article class="doc">
      <p class="eyebrow">{escape(eyebrow)}</p>
      <h1>{escape(title)}</h1>
      <p class="doc-intro">{intro}</p>
      {stamp}
      <nav class="doc-toc" aria-label="On this page"><ol>{toc}</ol></nav>{parts}
    </article>"""


# ---- Poly Account ----------------------------------------------------------------------------
ACCOUNT_ICONS = ("grid", "desktop", "laptop", "sync", "shield", "eye", "key", "bell", "help", "book", "logout", "plus", "check", "info")
for slug, start in (("account", "dashboard"), ("link", "link")):
    page(slug, "Poly Account", "Manage your PolyOS computers, recovery, sync and privacy.",
         '    <div id="app" class="acct-app" aria-live="polite"><p class="acct-loading">Loading your Poly Account…</p></div>',
         icons=ACCOUNT_ICONS, scripts=("/assets/account.js",), body_attrs=f' class="acct-page" data-start="{start}"', main_class="acct-wrap")

# ---- Download --------------------------------------------------------------------------------
page("download", "Download PolyOS 7", "Download the PolyOS 7 live USB and installer for PCs and ARM64.", f"""    <section class="section" id="download-page">
      <div class="section-head">
        <p class="eyebrow">Download</p>
        <h1 class="page-title">Get PolyOS 7</h1>
        <p>Free. Try it from a USB drive first, then install it on a drive of its own. No account needed.</p>
      </div>
      <div class="download" id="download">
        <img class="dl-logo" src="/assets/logo-white.svg" alt="" width="84" height="84">
        <div class="dl-main">
          <h3>PolyOS 7 <span class="dl-version" data-release-version></span></h3>
          <p class="dl-meta" data-release-meta>Live USB and installer · for PCs and ARM64 · Debian 13</p>
          <div class="dl-buttons" data-release-buttons>
            <a class="btn primary big" href="/download/pc" data-arch="amd64">{ico("download")}Download for PC (Intel/AMD)</a>
            <a class="btn big" href="/download/arm64" data-arch="arm64">{ico("download")}Download for ARM64</a>
          </div>
          <p class="dl-which"><b>Which one?</b> Almost every Windows PC and laptop: <b>PC (Intel/AMD)</b>.
            Apple Silicon Macs (in UTM, Parallels or VMware Fusion) and ARM64 computers with UEFI: <b>ARM64</b>.</p>
          <p class="dl-fresh">{ico("info")}<span><b>A fresh install.</b> PolyOS installs on a whole drive and replaces everything on the drive you choose,
            including Windows if it's there. Dual boot next to Windows isn't part of this download.</span></p>
          <p class="dl-note" data-release-note>Always the newest release.</p>
          <p class="dl-links"><a href="/download/checksums" data-release-sums>SHA256 checksums</a> · <a href="/install">Install guide</a> · <a href="/changelog">What's new</a></p>
        </div>
        <ul class="dl-needs" aria-label="What you need">
          <li><b>A 64-bit computer</b><span>Intel/AMD PC (UEFI or BIOS), or ARM64 with UEFI</span></li>
          <li><b>4 GB of RAM</b><span>2 GB works in light mode; 8 GB for gaming</span></li>
          <li><b>25 GB of disk</b><span>A whole drive: everything on it is replaced</span></li>
          <li><b>USB drive</b><span>8 GB or larger</span></li>
        </ul>
      </div>
      <ol class="steps">
        <li><b>Download the ISO</b><span>About 2 GB. The buttons always get the newest release.</span></li>
        <li><b>Write it to a USB drive</b><span>With balenaEtcher, or Rufus in DD mode.</span></li>
        <li><b>Start from the USB</b><span>Pick it in your computer's boot menu (F12, F9, F11 or Esc).</span></li>
        <li><b>Install PolyOS 7</b><span>Choose the drive: a fresh install replaces what's on it.</span></li>
      </ol>
      <p class="note">Step by step, with boot keys for every brand and fixes: <a href="/install"><b>the install guide</b></a>.
        Already using PolyOS? New versions install from Settings › About; no new USB drive needed.</p>
    </section>""", icons=("info",), scripts=("/assets/site.js",))

# ---- Documentation ---------------------------------------------------------------------------
SHORTCUTS = [("Win", "Start"), ("Win + S", "Search in Start"), ("Win + A", "Quick settings"), ("Win + W", "Widgets"), ("Win + V", "Ask Vara"), ("Win + P", "Duplicate, extend or one screen"),
             ("Win + R", "Run"), ("Win + I", "Settings"), ("Win + E", "Files"), ("Win + T", "Terminal"), ("Win + B", "Browser"),
             ("Win + L", "Lock"), ("Win + D", "Show the desktop"), ("Win + F", "Full screen on and off"), ("Win + ↑", "Maximize"),
             ("Win + ← / →", "Snap left or right"), ("Ctrl + Shift + Esc", "Task Manager"), ("Alt + Tab", "Switch windows"), ("Print", "Screenshot")]
shortcut_rows = "".join(f"<tr><td><kbd>{escape(k)}</kbd></td><td>{escape(v)}</td></tr>" for k, v in SHORTCUTS)
page("docs", "Documentation · PolyOS 7", "How to install, use and update PolyOS 7, and how Poly Account works.", prose(
    "Documentation", "Using PolyOS 7",
    'Everything from installing to updates. Stuck? See <a href="/support">Help &amp; Support</a>.', [
        ("Install", '<p>The <a href="/install">install guide</a> covers making the USB drive, starting from it, and what to do if '
                    'something goes wrong.</p>'
                    '<p><b>A fresh install:</b> PolyOS installs on a whole drive and replaces everything on it, including Windows if it\'s '
                    'there. Dual boot next to Windows isn\'t part of the download. After installing, PolyOS starts straight to your desktop.</p>'),
        ("First start", '<p>Setup happens once, on the USB drive, before PolyOS installs. It checks your computer first: the exact '
                        'model, processor, memory and graphics. Most computers get <b>Everything on</b>; older ones get <b>Smooth</b> or '
                        '<b>Light</b>, which turn off blur and see-through glass. Then your edition, Wi-Fi and, if you like, a '
                        '<a href="#poly-account">Poly Account</a>: sign in and you can use it to sign in to this computer too, with the same '
                        'name and password. Then your account (if you didn’t use your Poly Account) and your look. After the restart you go straight to your desktop; the recommended '
                        'drivers and your edition’s apps install in the background once you’re online, and a notification says when '
                        'they’re done.</p>'),
        ("Settings", '<ul><li><b>Display</b>: resolution, refresh rate, orientation and brightness; with a second screen, Duplicate, '
                     'Extend or one screen (also Win+P), remembered and set up again whenever you plug it in.</li>'
                     '<li><b>Sound</b>: output and input devices, microphone level, and the full mixer for per-app volume and surround.</li>'
                     '<li><b>Account</b>: your Poly Account, and how you sign in: a 4 to 6 digit <b>PIN</b> for the sign-in and lock '
                     'screens (installing and Terminal’s sudo still ask for the password), your password and a recovery key.</li>'
                     '<li><b>Personalization</b>: wallpaper, colors, dark or light.</li>'
                     '<li><b>Apps</b>: installed apps (uninstall with a button), startup apps, and usage: time in each app.</li>'
                     '<li><b>Storage</b>: how full your drives are, what your folders take, and clearing the trash and caches.</li>'
                     '<li><b>Power &amp; Performance</b>: power modes, what closing the lid and the power button do, the hardware check.</li>'
                     '<li><b>Updates</b>: automatic downloads, an install time, restart questions and the update channel.</li></ul>'
                     '<p><b>Quick settings</b> (the Wi-Fi, sound and battery icons): Wi-Fi, Airplane mode, Energy saver, Night light, '
                     'Sound, brightness and volume.</p>'),
        ("Updates", '<p>PolyOS updates itself from <b>Settings › Updates</b> (and <b>About › Update now</b>), with or without an account. '
                    'Updates are signed: PolyOS checks every update’s signature and checksums before installing it. You choose whether '
                    'updates download and install automatically, at what time, and whether PolyOS asks before restarting. Channels: '
                    '<b>Stable</b> for most people, <b>Beta</b> and <b>Developer</b> for early versions. Some updates are for one '
                    'edition only: PolyOS gets them only if this computer uses that edition or developer mode. Google Chrome '
                    'updates with the rest of the system.</p>'
                    '<p>If an update stops with “dpkg returned an error code (1)”, another package on the computer was left half '
                    'installed. PolyOS 1.2.2 and later repair that and try again by themselves, and name the package if it still '
                    'fails. On an older version, open Terminal and run <code>sudo dpkg --configure -a</code> then '
                    '<code>sudo apt-get -f install</code>, and install the update again. Details: <code>/var/log/polyos-update.log</code>.</p>'
                    '<p>Without internet, download the newest ISO from <a href="/download">the download page</a> and install over the old version.</p>'),
        ("Drivers", '<p><b>Driver Manager</b> shows your computer’s maker, model and firmware version, and every part: processor '
                    '(with its microcode updates), memory, drives, screens, NVIDIA, AMD and Intel graphics, Wi-Fi (including Broadcom), '
                    'Bluetooth, touchscreens and pens, and webcams. It installs what Debian has for them, and <b>firmware updates from '
                    'your computer’s maker</b> (BIOS/UEFI, SSDs, docks) through the Linux Vendor Firmware Service, where Dell, Lenovo, '
                    'HP and others publish them for Linux.</p>'),
        ("Poly Account", '<p>A Poly Account is optional. It adds device management, recovery, sync and account emails; PolyOS works fully without one.</p>'
                         '<ul><li><b>Connect a computer</b>: during setup, or Settings › Poly Account › Connect. Sign in there, or use the 6-digit code: '
                         'open <a href="/link">the link page</a> on any device and enter it.</li>'
                         '<li><b>Each computer has its own key</b>, so removing one (Devices › Remove device) doesn’t affect the others. The computer never keeps your password.</li>'
                         '<li><b>Remote management</b> (restart, lock and install updates from the website) is off until you turn it on at that computer.</li>'
                         '<li><b>Recovery</b>: a verified email resets your password; so does a recovery key from <a href="/account#recovery">Poly Account › Recovery</a>.</li>'
                         '<li><b>Poly Sync</b> keeps settings, theme, wallpapers and pinned apps the same on your computers. Files aren’t synced.</li></ul>'),
        ("Keyboard shortcuts", f'<table class="guide-table"><thead><tr><th>Keys</th><th>What it does</th></tr></thead><tbody>{shortcut_rows}</tbody></table>'),
        ("Vara", '<p>Vara is the PolyOS assistant and AI agent. Ask it from Start or with Win+V: it opens apps, changes the '
                 'volume, brightness and Wi-Fi, and writes and runs code, makes 3D models and works with ROS 2 robots and '
                 'Arduino boards, asking before it changes anything. Choose its provider (Ollama Cloud, OpenAI, NVIDIA or '
                 'Claude) in Settings › Vara.</p>'),
    ]), icons=("info",))

# ---- Updates / changelog ----------------------------------------------------------------------
page("changelog", "Updates · PolyOS 7", "What's new in each PolyOS release.", f"""    <article class="doc">
      <p class="eyebrow">Updates</p>
      <h1>What’s new in PolyOS</h1>
      <p class="doc-intro">Every release, newest first. PolyOS installs new versions from Settings › Updates; you can also <a href="/download">download the newest ISO</a>.</p>
      <div id="releases" class="release-list" aria-live="polite"><p class="muted">Loading releases…</p></div>
    </article>""", scripts=("/assets/changelog.js",))

# ---- Security Center ---------------------------------------------------------------------------
page("security", "Security · PolyOS 7", "How PolyOS and Poly Account keep you safe, and how to report a problem.", prose(
    "Security Center", "Security at Poly",
    "How PolyOS and Poly Account protect you, and how to tell us about a problem.", [
        ("PolyOS", '<ul><li><b>Firewall on</b>: other computers can’t connect in.</li>'
                   '<li><b>Debian security updates</b> install automatically.</li>'
                   '<li><b>Signed PolyOS updates</b>: each update is signed with Poly’s release key, and PolyOS refuses one whose signature or checksums don’t match.</li>'
                   '<li><b>One small administrator helper</b> does the few jobs that need administrator rights, and only those.</li>'
                   '<li><b>No remote access by default.</b> Remote management through Poly Account is off until you switch it on at the computer.</li></ul>'),
        ("Poly Account", '<ul><li>Passwords and recovery keys are stored only as salted, slow hashes (scrypt). Poly can’t read them.</li>'
                         '<li>Each connected computer has its own key, stored hashed and revocable on its own. Computers never keep your password.</li>'
                         '<li>Sign-in cookies are HTTP-only and secure; changes are only accepted from Poly’s own pages.</li>'
                         '<li>Repeated wrong passwords are slowed down, and we email you about new devices, password changes and new recovery keys.</li>'
                         '<li>See your active sessions and security activity in <a href="/account#security">Poly Account › Security</a>, and sign out other sessions there.</li></ul>'),
        ("Report a vulnerability", '<p>Found a security problem in PolyOS, the website or Poly Account? Tell us privately through '
                                   '<a href="/support?topic=security">Help &amp; Support</a> (choose <i>Security problem</i>). Please give us a chance to fix it before sharing it publicly. '
                                   'We read every report, reply as soon as we can, and credit you if you like.</p>'
                                   '<p>Please don’t access other people’s accounts or data, disrupt the service, or test against computers that aren’t yours.</p>'),
        ("Tips", '<ul><li>Use a unique password and save your recovery key somewhere safe.</li>'
                 '<li>Verify your email so you get security alerts.</li>'
                 '<li>Remove computers you no longer use from Poly Account › Devices.</li></ul>'),
    ]), icons=("shield",))

# ---- Privacy Policy -----------------------------------------------------------------------------
page("privacy", "Privacy Policy · PolyOS 7", "What PolyOS and Poly Account collect, and your choices.", prose(
    "Legal", "Privacy Policy",
    "PolyOS works without an online account, and collects nothing about you when you use it that way. This policy explains what "
    "happens when you use our website or choose to create a Poly Account.", [
        ("Using PolyOS without an account", '<p>When PolyOS checks for updates it sends only its version, update channel and architecture; '
                                             'no name, account or device identifier. Downloads come from GitHub, which, like any website, sees the address that connects to it.</p>'),
        ("What a Poly Account holds", '<ul><li><b>You give us</b>: your name, email address, country and password. We store the password and your recovery key only as one-way hashes.</li>'
                                      '<li><b>Your computers</b>: each one’s name, PolyOS version, architecture, update channel, last check-in and approximate location (city and country, worked out from the connection; we don’t store IP addresses). '
                                      'With device information set to Standard or Diagnostic, also its model, processor, memory, storage and graphics.</li>'
                                      '<li><b>Security records</b>: your sign-in sessions (browser type and approximate location) and account activity such as sign-ins, password changes and new devices.</li>'
                                      '<li><b>Poly Sync</b>: the settings you choose to sync.</li>'
                                      '<li><b>Messages you send us</b> through Help &amp; Support.</li></ul>'),
        ("How we use it", '<p>To run Poly Account: signing you in, managing your computers, sending updates and remote actions you ask for, syncing your settings, '
                          'keeping your account secure and emailing you about your account, security, updates and changes to these terms. '
                          'We don’t show ads, don’t sell or rent your information, and don’t use tracking cookies. The site uses one cookie, to keep you signed in.</p>'),
        ("Who helps us", '<p>Poly Account runs on Vercel (website and servers) with a Neon database; account emails are sent through Resend; PolyOS downloads are served by GitHub. '
                         'They process data for us to provide these services.</p>'),
        ("How long we keep it", '<p>Your account information stays until you delete your account. Sign-in sessions end after 30 days of inactivity; records used to slow down '
                                'password guessing are deleted after a day. Deleting your account removes your details, computers, synced settings and history right away.</p>'),
        ("Your choices and rights", '<ul><li>See and change your details in Poly Account › Account.</li><li>Choose how much device information is shared in Poly Account › Privacy.</li>'
                                    '<li>Choose which emails you get in Poly Account › Notifications (security alerts and important legal notices always go out).</li>'
                                    '<li>Download all your data in Poly Account › Privacy, and delete your account in Poly Account › Account.</li>'
                                    '<li>Questions or requests: <a href="/support?topic=account">contact us</a>.</li></ul>'),
        ("Children", '<p>Poly Account isn’t meant for children under 13, and we don’t knowingly collect their information. '
                     'If you believe a child has made an account, contact us and we’ll delete it.</p>'),
        ("Changes", '<p>When this policy changes in an important way, we’ll email you and ask you to review it the next time you sign in, showing what changed.</p>'),
    ], updated="Version 2026-09-26 · Effective September 26, 2026"), icons=("info",))

# ---- Terms of Service ---------------------------------------------------------------------------
page("terms", "Terms of Service · PolyOS 7", "The terms for using Poly Account and Poly's online services.", prose(
    "Legal", "Terms of Service",
    "These terms cover Poly Account and Poly’s online services. PolyOS itself doesn’t need an account.", [
        ("What changed", '<p><b>Version 2026-09-26</b>: the first version of these terms.</p>'),
        ("Your account", '<ul><li>You need to be at least 13 (or the age required where you live) to create a Poly Account.</li>'
                         '<li>Give accurate information and keep your password and recovery key safe. You’re responsible for what happens with your account.</li>'
                         '<li>Tell us right away if you think someone else has used your account.</li></ul>'),
        ("Using Poly services", '<p>Don’t misuse Poly services: no trying to access other people’s accounts or computers, disrupting the service, '
                                'sending harmful software, or using Poly Account for anything illegal. We may suspend accounts that do.</p>'),
        ("Your computers", '<p>Connecting a computer lets your account see its information and, only if you turn on Remote management at that computer, restart, lock or update it. '
                           'Only connect computers you own or are allowed to manage.</p>'),
        ("PolyOS", '<p>PolyOS is provided under its own license, shown in PolyOS and included with it. Nothing in these terms limits the rights that license gives you.</p>'),
        ("Availability", '<p>We work to keep Poly services running, but they’re provided “as is”, without warranties, and may change or be unavailable at times. '
                         'PolyOS keeps working without them.</p>'),
        ("Liability", '<p>To the extent the law allows, Poly isn’t liable for indirect or consequential losses, or for lost data, arising from using Poly services.</p>'),
        ("Ending", '<p>You can delete your account at any time in Poly Account › Account. We may close accounts that break these terms, and will tell you when we can.</p>'),
        ("Changes to these terms", '<p>If we change these terms in an important way, we’ll email you and show you what changed before asking you to accept them. '
                                   'Continuing to use Poly services after that means you accept the new terms.</p>'),
        ("Contact", '<p>Questions? <a href="/support">Contact us</a>.</p>'),
    ], updated="Version 2026-09-26 · Effective September 26, 2026"), icons=("info",))

# ---- Help & Support -----------------------------------------------------------------------------
FAQ = [
    ("Do I need a Poly Account?", "No. PolyOS works fully offline and updates without one. An account adds device management, recovery and sync."),
    ("How do I update PolyOS?", "Settings › Updates (or About › Update now). Without internet, download the newest ISO and install over it."),
    ("I forgot my Poly Account password", 'Use <a href="/account#forgot">Forgot password</a> (with a verified email) or <a href="/account#recover">your recovery key</a>.'),
    ("How do I remove a computer from my account?", 'Poly Account › Devices › Manage device › Remove device. PolyOS keeps working on it.'),
    ("Something doesn't work after installing", 'Open Driver Manager first; many problems are a missing driver. The <a href="/install#help">troubleshooting list</a> covers the rest.'),
]
faq = "".join(f"<details class=\"faq\"><summary>{escape(q)}</summary><p>{a}</p></details>" for q, a in FAQ)
page("support", "Help & Support · PolyOS 7", "Answers to common questions, and a way to contact the PolyOS team.", f"""    <article class="doc">
      <p class="eyebrow">Help &amp; Support</p>
      <h1>How can we help?</h1>
      <p class="doc-intro">Start with the <a href="/docs">documentation</a> and the <a href="/install">install guide</a>, or ask us below.</p>
      <section><h2>Common questions</h2>{faq}</section>
      <section id="contact"><h2>Contact us</h2>
        <form id="support-form" class="af-form support-form" novalidate>
          <label class="af-field"><span>What’s it about?</span>
            <select class="af-input" name="topic">
              <option value="install">Installing PolyOS</option><option value="bug">Something isn’t working</option>
              <option value="account">Poly Account</option><option value="security">Security problem (private)</option>
              <option value="idea">An idea</option><option value="other">Something else</option>
            </select></label>
          <label class="af-field"><span>Your email (so we can reply)</span><input class="af-input" type="email" name="email" autocomplete="email"></label>
          <label class="af-field"><span>Message</span><textarea class="af-input" name="message" rows="7" required minlength="10"
            placeholder="What happened, your computer’s model, and your PolyOS version (Settings › About)."></textarea></label>
          <p class="af-error" role="alert" hidden></p>
          <p class="af-ok" hidden>Thanks, we got your message.</p>
          <button class="btn primary" type="submit">Send</button>
        </form>
      </section>
    </article>""", scripts=("/assets/support.js",))

# ---- 404 ---------------------------------------------------------------------------------------
page("404", "Not found · PolyOS 7", "This page doesn't exist.", """    <section class="section notfound">
      <img src="/assets/logo-white.svg" alt="" width="64" height="64">
      <h1>This page isn’t here</h1>
      <p>It may have moved. Try the <a href="/">home page</a>, <a href="/download">downloads</a> or <a href="/docs">documentation</a>.</p>
    </section>""")

# ---- the same header and footer on the hand-written pages ----------------------------------------
for name in ("index.html", "install.html"):
    path = DOCS / name
    text = path.read_text("utf-8")
    text = re.sub(r'<header class="nav" id="top">.*?</header>', HEADER, text, count=1, flags=re.S)
    text = re.sub(r"<footer class=\"footer[^\"]*\">.*?</footer>", FOOTER, text, count=1, flags=re.S)
    for needed in ("user",):
        if f'id="i-{needed}"' not in text:
            text = text.replace("<defs>", f'<defs>\n      <symbol id="i-{needed}" viewBox="0 0 24 24">{ICONS[needed]}</symbol>', 1)
    path.write_text(text, "utf-8")
    print(f"  updated docs/{name}")
