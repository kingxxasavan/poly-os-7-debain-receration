# PolyOS for Debian

PolyOS started as an operating system built in Scratch by AndrewInput and the PIXAPoLY team
([scratch.mit.edu/users/PolyOS](https://scratch.mit.edu/users/PolyOS/)). This project makes it a
real desktop: a PolyOS session that runs on top of Debian and boots on real hardware.

Debian provides the kernel, drivers, Wi-Fi, audio and apps. PolyOS provides everything you see:
the desktop, taskbar, Start menu, Files, Settings, Task Manager, Driver Manager, the
PolyMarket app store, Ask Vara, the login and lock screen, the boot splash and the PolyOS 7
installer on the live USB. This edition is presented by Cryptic Software.

**See it:** [`docs/`](docs/) is the project website, with screenshots of everything so far
(open `docs/index.html`, or publish it with GitHub Pages as described in [`docs/README.md`](docs/README.md)).

## How it fits together

```
LightDM + polyos-greeter                  PolyOS login and lock screen (WebKit)
  └─ polyos-session                      X session: env, HiDPI scale, dark/light theme, supervision
       ├─ openbox                        window manager (PolyOS theme, keybindings)
       ├─ picom                          compositor: blur, shadows, rounded corners (optional)
       ├─ xcape, polkit agent
       └─ polyos-shell                   restarted automatically if it crashes
            ├─ GTK windows + WebKit      desktop · dock · popups · Settings · Files · Task Manager ·
            │                            Driver Manager · PolyMarket · setup/installer  →  ui/
            ├─ libwnck                   tracks app windows for the dock
            ├─ Gio                       apps from .desktop files, launching, XDG autostart
            └─ 127.0.0.1 API + events    token-protected bridge between the UI and the system
                 ├─ system.py            NetworkManager, PipeWire, brightnessctl, logind
                 ├─ files.py             Files app: filesystem and freedesktop Trash
                 ├─ procs.py             Task Manager: processes and performance from /proc
                 ├─ drivers.py, store.py Driver Manager and PolyMarket catalogs
                 └─ vara*.py             Vara: local actions, the agent loop, tools, skills, memory
polyos-ctl                               CLI used by keybindings and scripts
polyos-admin (root, via sudo)            installer engine (installer.py), apt and Flathub installs
```

The UI is plain HTML/CSS/JS with no build step. On Debian it runs inside WebKitGTK. With
`python main.py dev` it runs in any browser against a simulated system, so design work doesn't
need a Linux machine.

## Commands

Everything goes through `main.py`, which uses only the Python standard library.

| Command | Where | What it does |
|---|---|---|
| `python main.py dev` | any OS | UI at http://127.0.0.1:8790 with fake apps, windows, Wi-Fi and battery (`--live` runs the installer on fake disks) |
| `python main.py test` | any OS | unit tests (parsers, settings, popup logic, HTTP security, .deb format) |
| `python main.py check` | any OS | syntax checks for Python/JS/XML/SVG, LF line endings |
| `python main.py deb` | any OS | builds `dist/polyos-shell_*.deb` and `dist/polyos-desktop_*.deb` |
| `sudo python3 main.py install` | Debian | builds and installs the packages, hands networking to NetworkManager, makes PolyOS your default session |
| `sudo python3 main.py uninstall` | Debian | removes the packages |
| `sudo python3 main.py iso` | Debian | builds `dist/polyos-<version>-trixie-amd64.iso`: a live USB with the Calamares installer |
| `python3 main.py nested` | Debian desktop | runs the real session in a Xephyr window for development |
| `python main.py branding` | any OS | re-renders the crystal backdrop, boot splash and installer images (needs Pillow) |
| `python main.py deb --manifest` | any OS | also writes `polyos-update.json` (and signs it when `POLYOS_UPDATE_KEY` holds the release key) |
| `npm install && npm run dev` | Node 20+ | the website and Poly Account API at http://localhost:8792 (PGlite stands in for the database) |
| `npm test` | Node 20+ | Poly Account API tests |
| `python3 tools/website_pages.py` | any OS | rewrites the website's secondary pages and the shared header and footer |

`POLYOS_SERVER=http://localhost:8792 python main.py dev` connects the PolyOS preview to that local
website, so Poly Account, check-ins, remote actions and sync can be tried end to end.

## Install on a PC or VM

1. Install **Debian 13 (trixie)** from the netinst ISO. At *Software selection*, uncheck every
   desktop and keep only **standard system utilities** (and SSH server if you want it).
   Debian 12 (bookworm) also works.
2. Sign in and get the project onto the machine:
   ```bash
   sudo apt install git python3
   git clone <your-repo-url> PolyOS && cd PolyOS
   ```
   Or copy the folder over with a USB stick or `scp`.
3. Install:
   ```bash
   sudo python3 main.py install
   ```
   This pulls in Xorg, LightDM, Openbox, picom, PipeWire, NetworkManager, fonts, icons, a browser
   (Chromium here; the PolyOS ISO has Google Chrome), a terminal and a text editor, around 1 GB in total.
4. Reboot. The PolyOS login screen comes up; sign in.

Already have GNOME, Xfce or another desktop? Run `sudo python3 main.py install --shell-only`,
then pick **PolyOS** from the session menu on your current login screen.

**Networking note:** when Debian is installed without a desktop, it configures the network in
`/etc/network/interfaces`, and NetworkManager ignores those interfaces. `install` moves them to
NetworkManager (the old file is kept as `interfaces.polyos-backup`). If you were on Wi-Fi, enter
the password again from the PolyOS Wi-Fi menu after the reboot. Pass `--keep-ifupdown` to skip
this step.

## What's in the desktop

| Piece | What it does |
|---|---|
| **Installer** | The PolyOS 7 setup on the live USB: "Cryptic Software presents", the falling pinwheel and 7, the crystal welcome, then *Install PolyOS 7* or *Dual boot* (next to Windows or Linux, shrinking it if needed, or *Choose a partition instead* for a D: drive you made; while BitLocker is on, or while Windows is only asleep because of Fast Startup, it explains how to fix that first, since changing Windows' drive then sends Windows into Automatic Repair), *Checking your computer*, terms, edition, Wi-Fi, **Poly Account** (optional; signed in, *Use this account to sign in* makes your Poly Account name and password this computer's sign-in, like a Microsoft account on Windows), then your account (only the username, computer name and time zone when you use your Poly Account), dark/light appearance, then “Where do you want to install PolyOS?”: like Windows Setup, every drive, partition and unallocated space in one list with Refresh, Delete and New, and PolyOS goes in the partition (erased) or space you pick; *Advanced setup* sets mount points per partition. Progress, then *Restart now* with the USB drive still in. Everything is asked before installing: the installed PolyOS starts straight to the sign-in screen. Calamares stays as the *Advanced installer* |
| **Custom install** | Choose what every drive and partition is for: erase a drive for PolyOS, your files (/home) or extra storage; or use existing partitions for PolyOS (/), /home (keeping its files), the EFI boot partition, swap, or storage at /mnt/NAME (NTFS drives too). Anything left on *Keep* isn't touched, and a summary lists every erase before you confirm. Impossible choices are refused before anything is erased |
| **Editions** | *Regular*, *Developer* or *Gaming*, picked while installing; the edition's apps install by themselves after installing, once online, and can be added any time in Settings |
| **Gaming** | Steam, Bottles (Wine for Windows games), Heroic, Lutris, ProtonUp-Qt, GameMode, Discord; cloud gaming apps (GeForce NOW, Xbox Cloud Gaming, Amazon Luna, Boosteroid) built in and hidden until you switch them on in Settings > Gaming (they open in Chromium, which comes with PolyOS); `vm.max_map_count` raised like SteamOS; **Game Mode**: while a game is full screen, the performance power mode, no idle lock or sleep, slower status polling, background helpers at low priority, and the compositor steps aside |
| **Developer** | Developer mode: files in `~/.config/polyos/ui/` replace PolyOS's built-in interface files (`css/user.css` is added to every screen), `~/PolyOS-UI` holds a copy of the originals, right-click *Inspect Element* on PolyOS screens, *Reload the interface*. The Developer edition installs Git, build tools, Python with pip, venv and its main libraries (NumPy, pandas, requests, …), Node.js and npm, VS Code and Docker, with Blender and OpenSCAD as options; every PolyOS has Git, pip, venv and build-essential. `polyos-ctl dev off` (Ctrl+Alt+T) undoes a broken change |
| **Security** | Firewall (ufw) on by default, automatic security updates, a security checkup, and the lock screen slows down guessing. See [Security model](#security-model): the interface is not the boundary; everything done as root goes through `polyos-admin`, which checks every request itself |
| **Speed** | Compressed RAM swap (zram), SSD trim, capped system log, no waiting for the network at startup, full-screen apps bypass the compositor, one shared event connection for all PolyOS screens |
| **First start** | Straight to the desktop. What setup chose that needs the internet (the recommended drivers and the edition's apps) installs in the background (polyos-first-start.timer, every 5 minutes until done), with a notification when it starts and when it's all set up |
| **Dock** | The taskbar: pinwheel (Start), widgets, pinned and running apps, **^** (running apps, including ones in the background like Steam or Discord, with Close and Quit), quick settings, clock; right-click an app for Close, Force close and Task Manager |
| **Home Menu** | The **Start menu**, laid out like Windows 11's: search (apps, settings and your recent files), *Pinned* (right-click any app to pin it to Start or the taskbar, or put it on the desktop), *All apps* A to Z, *Recommended* (files and apps you opened lately), and you, Ask Vara, Files, Settings and Power (Lock, Sleep, Sign out, Restart, Shut down, and *Restart to Windows* on dual-boot computers). The Windows key and Win+S open it |
| **Launcher** | Only the everyday apps are listed: of what the PolyOS image ships, Google Chrome (Chromium on ARM), Files, Terminal, Text Editor, Calculator, Photos, Document Viewer, Bluetooth and PolyOS's own; XTerm, screenshot and settings tools stay out (the ISO records its apps in `/usr/share/polyos/base-apps.txt`; apps you install later always show). Settings > Personalization > Show all apps lists everything |
| **Desktop** | The PolyOS 7 crystal wallpaper with app shortcuts: double-click to open (or single click, in Settings), right-click to pin or remove. Add apps by right-clicking the desktop (*Add apps to the desktop*) or any app in the launcher (*Add to desktop*) |
| **Desktop menu** | Right-click (or two-finger tap): Add apps to the desktop, Taskbar settings, Personalization, Display settings, Task Manager, Terminal, Files, PolyMarket |
| **Files** | File manager for the real disk: places, breadcrumbs, grid/list views, thumbnails, search, copy/cut/paste, **drag and drop** (onto folders, the places on the left or the Trash; Ctrl copies; between Files windows too), rename, *Set as wallpaper* / *Set as lock screen* for pictures, Trash with restore, properties |
| **Task Manager** | Apps and processes with CPU and memory, End task and Force close, live CPU/memory/disk/network graphs |
| **Driver Manager** | Shows this computer's maker, model and BIOS/UEFI version; **firmware updates from the maker** (BIOS/UEFI, SSDs, docks, touchpads) through fwupd and the Linux Vendor Firmware Service, where Dell, Lenovo, HP and others publish them; and every part (processor with its microcode updates, memory, drives, screens, graphics, Wi-Fi, Bluetooth, sound, touchscreens and pens, webcams and Intel IPU6 cameras) with the right drivers and firmware from Debian |
| **PolyMarket** | Curated store: Chrome (built in), Discord, Spotify, Steam, VS Code, LibreOffice, GIMP, OBS and more from Debian and Flathub. *Install* several at once: each waits its turn (*Queued*, click to cancel) |
| **Settings** | In this order: **Display** (resolution, refresh rate, orientation per screen with a 15-second undo; with more than one screen *Duplicate*, *Extend* or one screen, also on Win+P, remembered and set up again whenever a screen is plugged in; brightness, scale, graphics), **Sound**, **Account** (your Poly Account and how you sign in: PIN, password, recovery key), Privacy & Security, Wi-Fi & Network, **Personalization**, Taskbar & Desktop, Gaming, Vara, **Apps** (*Installed apps* with uninstall, *Startup apps*, *Usage*: time in each app today and this week, kept on the computer), **Storage** (how full each drive is, what your folders take, empty the trash, clear thumbnails and downloaded packages), **Power & Performance** (power modes, *When I close the lid* on battery and plugged in, the power button, the hardware check, screen-off and sleep timers), Updates, Developer (with developer mode on), About |
| **Full screen** | The title bar's middle button, Win+F or *Full screen* in the taskbar's right-click menu shows only the app: no title bar, no taskbar. Nothing pops up over a full-screen app; Win+F (or the app's own F11 or Esc) leaves full screen. Double-click the title bar (or Win+Up) to maximize instead |
| **Hardware check** | When PolyOS first starts it checks the processor, memory and graphics and says how well the PC fits. Most get *Everything on*; PCs with software graphics get *Smooth* (no blur); 2–3 GB or single-core PCs get *Light*: no blur, shadows or see-through glass, quicker animations, fewer widgets. Change it or check again in Settings > Power & Performance. It also reads the exact model and firmware year (e.g. *Lenovo IdeaPad 5 15ALC05*, no serial number needed), eases off on computers eight or more years old, and recommends *Limit background activity* on small or older laptops (Vara's programs at low priority, fewer status checks and widget refreshes) |
| **Updates** | Settings > Updates: the update service (a systemd timer) checks every few hours, downloads in the background and installs only at your preferred time (automatic install, or *Install tonight*), with *Install now*, *Check now*, *Ask before restarting* and Stable/Beta/Developer channels, a calm “PolyOS 1.0.0 is ready” notice, and “PolyOS was updated to …” (with *What's new*) once it's running the new version. Every update is **signed**: PolyOS checks the manifest's Ed25519 signature and each package's checksum before installing anything. Works with or without an account; update checks send only the version, channel and architecture. *Update everything* installs Debian's updates. Installed computers that share with Windows get a PolyOS-themed boot menu |
| **Poly Account** | Optional, never required. Connect during setup or in Settings > Poly Account (sign in, create an account, or a 6-digit code entered at /link), or keep using PolyOS locally with no prompts. Connected computers show up at /account (Dashboard, Devices, Updates, Sync, Security, Privacy, Recovery, Notifications, Account); Poly Sync keeps settings the same; Remote management (restart, lock, install updates from the website) is off until switched on at the computer. Each computer has its own revocable credential and never keeps the password |
| **Trying it** | On the USB drive, *Try PolyOS first* clears the desktop; *Install PolyOS 7* in the dock and on the desktop goes back to the installer |
| **Camera** | Photos and videos from the webcam (self-timer, mirror, switch camera), saved to Pictures › Camera. Always in Start; it says so when no camera is connected |
| **Vara** | AI agent for code, 3D and robots (see [Vara, the agent](#vara-the-agent)); simple requests ("open chrome", "volume 40", "turn wifi off") run right on the PC. Ask Vara from Start or Win+V |
| **Login screen** | PolyOS 7 design over the blurred amethyst crystal: big stacked clock, date, news and notification tiles, *Click to Enter Password*, then your name and your **PIN or password**. Falls back to the stock greeter if it can't start |
| **Lock screen** | Same design; appears instantly (Win+L, the power menu, before sleep, when the screen turns off); unlocks with your PIN or password |
| **Forgot password** | Settings > Account makes a recovery key if you want one (it isn't part of setup any more); with it you set a new password from the login or lock screen |
| **Widgets** | Win+W or the weather button in the dock: weather (Open-Meteo), calendar, system, BBC news, to-do, notes, photos, world clocks, media controls |
| **Boot splash** | Spinning pinwheel (Plymouth) |
| **Boot menu** | Installed PolyOS starts straight away, with no menu to wait on. With Windows too, hold Shift or press Esc while it starts for the menu, or use Start › Power › *Restart to Windows* (it picks Windows for that one start) |
| **Quick settings** | Wi-Fi, Airplane mode, Energy saver, Night light (warmer colors) and Sound, brightness and volume, and the battery; power and Settings are in Start |
| **USB boot menu** | PolyOS background, *Start PolyOS 7* and *Start PolyOS 7 (safe mode)*, starts by itself after 5 seconds |
| **App icons** | Real icons from the Papirus theme for installed apps, PolyMarket and the dev preview |

## Vara, the agent

Vara is an AI agent built into the desktop, in the spirit of coding agents like OpenClaw and Hermes Agent
but aimed at making things: software, 3D models and prints, electronics and robots. Open it from the Home
Menu (Ask Vara) or with `Super+V`, and describe what you want.

It works in a loop: the model reasons about the request, calls a tool, reads the result, and carries on
until the job is done, then sums up. Each step shows in the chat as a card you can expand.

| Tool | What it does | Needs your OK |
|---|---|---|
| `list_files`, `read_file`, `search_files` | Look through folders and files | No |
| `write_file`, `edit_file` | Create and change files | Yes |
| `run_command` | Run a bash command (builds, tests, pip, npm, colcon) | Yes |
| `git` | status/diff/log look; commit, push and the rest change things | Only to change |
| `openscad` | Render OpenSCAD code to STL/3MF/PNG, report the model size | Yes (writes a file) |
| `blender` | Run a bpy script in Blender without its window (model, import/export, render) | Yes |
| `model_info` | Size and triangle count of an STL/OBJ | No |
| `ros2` | ROS 2 CLI: topics, nodes, params look; run, launch, pub, service calls move robots | Only to act |
| `arduino` | arduino-cli: board list looks; compile writes; upload and installs act | Compile and act |
| `open`, `list_windows` | Open apps, files or VS Code; see what's on screen | No |
| `fetch_url` | Read docs and datasheets from the web | No |
| `remember`, `load_skill`, `save_skill` | Memory and skills | Only to save a skill |

Tools for programs that aren't installed are left out; Vara says which PolyMarket app provides them
(Blender, OpenSCAD, FreeCAD, KiCad, PrusaSlicer, Cura and the Arduino IDE are there).

**Approvals.** Settings > Vara > *Ask before changes*: **Always** (default), **Only outside the
workspace** (edits inside `~/Projects` need no OK; commands still do) or **Never**. The approval card
shows the exact command, script or file content, with *Allow*, *Always in this chat* and *Deny*; if the
chat is closed when Vara needs an answer, it opens. *Stop* ends a request at once, including a running
command. Vara runs as you, never as root, and never opens SSH/GPG keys, saved passwords, browser
profiles or its own API key.

**Context.** Every request carries what Vara needs to reason about this computer: PolyOS version,
architecture, the open windows, the workspace folder, which development tools are installed, its
skills and its memory.

**Skills** are Markdown how-tos Vara loads when a task needs one. PolyOS ships `blender`, `openscad`,
`3d-printing`, `freecad`, `ros2`, `arduino`, `python-project` and `git` (`data/vara/skills/`). Add
your own as `~/.config/polyos/vara/skills/<name>/SKILL.md` (or `<name>.md`):

```markdown
---
name: my-printer
description: Slice for my Ender 3 with my usual settings
---
1. Use `prusa-slicer --load ~/printers/ender3.ini ...`
```

Vara can also save a skill itself after working something out, with your OK. **Memory** holds short
lasting notes (your board, printer, where projects live); Settings > Vara lists them and forgets them.

**Models.** Pick a provider in Settings > Vara: Ollama Cloud (`gpt-oss:120b`, the default), OpenAI,
NVIDIA (`meta/llama-3.3-70b-instruct` and the other models on build.nvidia.com), Claude (`claude-opus-5`
through Anthropic's official Python library, with adaptive thinking), or any other OpenAI-compatible
service with tool calling. Models without tool calling still chat.

## Security model

```
            PolyOS
              |
      +-------+--------+
      |                |
  Poly shell       polyos-admin (root)        polkit / sudo / PAM
  web UI (WebKit)  checks every request        (the password)
      |                |
      +-------+--------+
              |
        Linux / Debian
```

- **The web UI is not the security boundary.** The shell's HTTP bridge listens on 127.0.0.1 only,
  refuses foreign Host headers and needs a per-session token that only the WebKit views hold (in
  memory, never on disk). Anything done as root goes through `polyos-admin`, which accepts a fixed
  set of commands and checks each one itself (catalog apps only, driver names by pattern, signed
  updates only, partitions it probed itself).
- **polyos-ctl** (keybindings, the command line) has its own token, in the session's runtime file,
  and it opens only popups, apps, volume, brightness and power (`CTL_API` in `polyos/server.py`), so
  another program running as you can't install apps or change accounts through the shell.
- **The administrator password** is asked in a PolyOS dialog, kept in the shell's memory for five
  minutes and handed to each `polyos-admin` run (`sudo -S -k`); sudo never caches it, so an app or a
  command Vara runs can't use sudo without asking. Locking the screen forgets it.
- **Vara** is an assistant, not an administrator: it runs as you, never as root; every change and
  every command goes through its approval card (Settings > Vara > *Ask before changes*), and it
  never opens SSH keys, saved passwords, browser data or its own API key. It has no tool for
  PolyOS's privileged side.
- **PIN sign-in** (Settings > Account): 4 to 6 digits for the sign-in and lock screens only. Only an
  scrypt hash is kept, in `/var/lib/polyos/pin` (root only); a root service (`polyos-pin.socket`)
  checks it for the lock screen, knows the caller from the socket, and turns the PIN off after 5
  wrong tries until the password is used. sudo, pkexec and installing use the password, always.

## Build the live USB / installer ISO

**Just want the ISO?** Step-by-step help (BitLocker, boot menu keys, dual boot, problems) is in the [install guide](docs/install.html). Download the newest release from the website's Download buttons or from
[GitHub Releases](https://github.com/kingxxasavan/poly-os-7-debain-receration/releases/latest). Each release has two:
`polyos-amd64.iso` for Intel/AMD PCs and `polyos-arm64.iso` for ARM64 virtual machines (Apple Silicon Macs
in UTM, Parallels or VMware Fusion; ARM cloud servers). `sudo python3 main.py iso` builds the one for
the computer it runs on (`--arch`); GitHub Actions builds both.

**Publishing a new release is automatic:** raise `__version__` in `polyos/__init__.py` (say
`0.6.0` → `0.6.1`), write what changed in `RELEASE_NOTES.md` (Settings › Updates, the update
notice and the website's release notes show it), and push both to `main`. GitHub builds both ISOs and, about 15 minutes later,
publishes them with a `SHA256SUMS` file as release `v<version>` (split into parts if one is ever
over GitHub's 2 GB file limit). The website's Download buttons offer the new release as soon as it
appears; nothing on the website needs changing. Pushes that don't change the version don't build
anything, and a version that's already released isn't built again. To publish by hand instead, run
**Actions → Build PolyOS ISO → Run workflow** with **Publish as a GitHub Release** ticked, or push
a tag like `v0.7.0`.

An ISO has to be built on Linux. Three ways:

**1. GitHub Actions (no Linux needed).** Push this folder to a GitHub repository, open
**Actions → Build PolyOS ISO → Run workflow**, and download the ISO from the finished run
(about 30–60 minutes). A public repository keeps the artifact storage free; private repositories
on the free plan only get 500 MB of artifact storage, less than one ISO.

**2. WSL on Windows.** Install Debian once (`wsl --install -d Debian` in an admin terminal, then
reboot if asked), then inside Debian:

```bash
sudo apt update && sudo apt install -y live-build python3
cd /mnt/c/Users/<you>/PolyOS
sudo python3 main.py iso
```

The build runs in `~/polyos-iso` inside WSL (a Linux chroot can't live on a Windows drive) and the
finished ISO lands in `dist\` on the Windows side.

**3. Any Debian 12/13 machine or VM** with about 20 GB free:

```bash
sudo apt install live-build
sudo python3 main.py iso            # --dist bookworm for Debian 12
```

The ISO boots into a live PolyOS session as user `polyos` (no password) and opens the PolyOS 7
installer. *Try PolyOS first* goes to the desktop; the **Install PolyOS 7** card brings the
installer back. Installing needs no internet: the GRUB packages for UEFI and legacy BIOS are on
the USB drive. Firmware for common Wi-Fi chips (Intel, Realtek, Atheros, Broadcom, Marvell for
Surface devices) is included.

**Dual boot with Windows:** turn off BitLocker and Fast Startup in Windows first (Control Panel >
Power Options > Choose what the power buttons do), and back up your files. The installer shrinks
Windows' partition for you, or uses free space you made in Windows' Disk Management. The boot menu
then offers PolyOS and Windows.

## Test the ISO

**VirtualBox.** New VM → type *Linux*, version *Debian (64-bit)*; 4 GB RAM, 2 CPUs, 25 GB disk.
Under *Display*, choose **VMSVGA** with 128 MB video memory (leave 3D off; PolyOS falls back to
software effects). Attach the ISO as the optical drive and start. Try the live session, then
**Install PolyOS**, restart and sign in on the PolyOS login screen: setup already happened before installing.

**An old laptop.** Write the ISO to a USB stick with [Rufus](https://rufus.ie) (choose *DD image*
mode when asked) or [balenaEtcher](https://etcher.balena.io). Boot the laptop from USB (usually
F12, F9, F2 or Esc at power-on). If it won't boot the stick, turn off Secure Boot in the firmware
settings. The live session changes nothing on the disk until you run the installer.

## Keyboard

| Keys | Action |
|---|---|
| Tap `Super` (Windows key) · `Super+Space` · `Super+S` | Open or close Start (type to search) |
| `Super+P` | Duplicate, extend or one screen |
| `Super+R` | Run CMD |
| `Super+X` | Quick menu: Task Manager, Settings, Files, Driver Manager, PolyMarket, power |
| `Super+W` | Widgets |
| `Ctrl+Shift+Esc` · `Ctrl+Alt+Delete` | Task Manager |
| `Super+V` | Ask Vara |
| `Super+A` | Quick settings |
| Power key | Power Options |
| `Super+I` | Settings |
| `Super+E` / `Super+T` / `Super+B` | Files / Terminal / Browser |
| `Super+L` | Lock |
| `Ctrl+Alt+T` | Terminal (works even if the PolyOS interface is broken: `polyos-ctl dev off`) |
| `Super+D` | Show desktop |
| `Super+←` `Super+→` `Super+↑` `Super+↓` | Snap left/right, maximize, restore/minimize |
| `Alt+Tab` · `Alt+F4` | Switch · close windows |
| Volume, brightness and power keys | Work, even if the shell is down |

Touchpads: tap to click, two-finger tap to right-click, natural scrolling (like Windows).

## Where things live

| Path | Contents |
|---|---|
| `polyos/` | Python: `shell.py` (GTK/WebKit/wnck), `server.py` (API), `system.py` (hardware), `session.py`, `ctl.py`, `installer.py` + `admin.py` (root helper), `privileged.py` (sudo, jobs), `procs.py`, `drivers.py`, `store.py`, `mock.py` (dev) |
| `data/store/catalog.json` | PolyMarket's app list (also the list of what may be installed) |
| `/var/log/polyos-installer.log` | Installer log (copied to the installed system) |
| `ui/` | The interface: `js/surfaces/` (desktop, panel, popup, settings), `js/views/` (Home Menu, launcher, Run CMD, Power Options, quick settings, calendar, task menu), `css/polyos.css` |
| `data/` | Openbox config and theme, picom, GTK defaults, LightDM greeter, session file, wallpapers, entry-point scripts |
| `iso/config/` | live-build additions: package list, GRUB branding, the USB boot menu hook |
| `data/vara/skills/` | Vara's built-in skills |
| `~/.config/polyos/vara.json` | Vara's provider, API key (mode 600), workspace and approval setting |
| `~/.config/polyos/vara/` | Your skills (`skills/`) and Vara's memory (`memory.json`) |
| `ui/img/apps/` | Papirus app icons (GPL-3.0) for apps whose icon the theme lacks |
| `~/.config/polyos/settings.json` | Per-user settings (the Settings app writes this) |
| `~/.config/polyos/openbox/rc.xml`, `~/.config/polyos/picom.conf` | Optional per-user overrides |
| `~/.local/state/polyos/*.log` | Session, shell and helper logs |

`polyos-ctl status` prints the live shell state. `polyos-ctl restart` restarts the shell without
closing apps.

## Current limits

- Vara's tools run commands without a terminal, so interactive programs (editors, `sudo` password
  prompts, `ros2 run` of a node that runs forever) stop at the time limit; Vara suggests running those
  in a Terminal. ROS 2 isn't packaged for Debian: Vara uses it when it's installed (RoboStack, a
  container or a source build). How well Vara plans depends on the model; small local models make
  more mistakes.
- ARM64: starts in virtual machines and from a USB stick on computers with UEFI firmware (boards
  with only U-Boot or a vendor bootloader can't start it; a Raspberry Pi 4 needs the community UEFI
  firmware). Apps built only
  for Intel/AMD PCs (Steam, Discord, Spotify, Chrome and a few more) aren't offered there; cloud
  gaming works in the browser instead.
- Poly Account: two-step sign-in, passkeys and Wi-Fi/browser sync aren't there yet; syncing files
  isn't planned for Poly Sync (that would be a separate service).
- Custom installs use existing partitions or whole drives; they don't create or resize single
  partitions (Dual boot does that, or use the Advanced installer). Disk encryption isn't offered yet.
- Edition apps and drivers come from Debian and Flathub, so they install once the computer is online after installing. Packages a
  Debian release doesn't have are skipped and named in the result.
- Developer-mode overrides replace whole files; after a PolyOS update, compare your copies with
  `~/PolyOS-UI` (Settings > Developer > Copy to my files).
- X11 only; Wayland would mean replacing Openbox and libwnck. The taskbar and popups use the
  primary monitor; the wallpaper stretches across every monitor.
- Full-screen apps (videos, games, F11) cover the taskbar. Maximized windows stop above it
  unless the taskbar is set to hide automatically (Settings > Taskbar & Desktop).
- Power modes use power-profiles-daemon when the computer supports it; otherwise only the
  screen and sleep timers change. Idle timers need libxss1 (installed with PolyOS).
- The Camera app uses WebKitGTK's GStreamer webcam support; recording video needs
  gstreamer1.0-plugins-good (installed with PolyOS).
- The installer, Driver Manager and PolyMarket run their root steps through `polyos-admin`; the
  partition planning is unit-tested and the steps have a dry-run test, but test installs on a
  spare disk or VM before trusting one with important data.
- Apps and drivers installed in the live session disappear at restart (it runs from RAM).
- The login screen can't read each user's settings (users' home folders are private), so it
  shows the default Amethyst background; the lock screen uses the one chosen in Settings.
- The look follows PolyOS 7: the logo is the original vector, and the palette was measured from
  the Scratch costumes (`:root` in `ui/css/polyos.css`). The two crystal photos are third-party
  images; check their license before sharing builds publicly (see `CREDITS.md`).

## Website, Poly Account and releases

The website is `docs/` (static pages) plus one Vercel function, `api/index.js` → `server/app.js`,
the Poly Account API; `vercel.json` sends `/api/…` and `/download/…` to it. Poly Account data lives
in Postgres. PolyOS itself never needs any of this: updates keep working if the website is down.

**Vercel settings** (Project > Settings > Environment Variables):

| Name | What it's for |
|---|---|
| `DATABASE_URL` | Set for you when you add a Neon Postgres database (Storage > Create Database). Tables are created on first use |
| `RESEND_API_KEY`, `EMAIL_FROM` | Account emails (verification, password reset, security alerts) through resend.com, e.g. `Poly <accounts@your-domain>`. Without them, accounts work but no emails are sent |
| `RELEASES_REPO` | The public repository with the releases (default: this one) |
| `GITHUB_TOKEN` | Optional: a read-only token, so release lookups aren't rate-limited |
| `SITE_URL` | Optional: the site's address for links in emails (default: the address it was reached on) |

**GitHub settings** for releases (Settings > Secrets and variables > Actions): the secret
`POLYOS_UPDATE_KEY` (Poly's Ed25519 release key, PEM) signs every release's `polyos-update.json`;
PolyOS 0.9 and later only install signed updates, checked against `TRUSTED_KEYS` in
`polyos/updates.py`. To keep this repository private, create a public repository for releases,
set the variable `RELEASES_REPO` to its name and the secret `RELEASES_TOKEN` to a token that can
create releases there (and set the same `RELEASES_REPO` on Vercel). To change the release key,
add the new public key to `TRUSTED_KEYS` one release before switching the secret.

**Updates for one edition.** Set `__editions__ = ("developer",)` in `polyos/__init__.py` (next to
`__version__`) to send a release only to Developer edition computers. The workflow writes the
editions into the signed `polyos-update.json` and publishes the release as a GitHub pre-release;
the update server offers it only to PolyOS 1.2.2 and later that report that edition (a computer is
Developer if anyone on it uses the Developer edition or developer mode, see
`updates.local_edition()`), and the computer refuses a manifest that isn't for its edition.
Everyone else stays on the newest release for everyone. Leave it `()` for a release for everyone.

**When an update fails** ("dpkg returned an error code (1)"): the whole apt output is in
`/var/log/polyos-update.log`. Since 1.2.2 `polyos-admin` finishes half-installed packages
(`dpkg --configure -a`, `apt-get -f install`) before and after a failed install, tries once more,
and names a package that still blocks it.

## Credits and license

Based on PolyOS by AndrewInput and PIXAPoLY Software, used with their blessing. Code: GNU GPL v3
or later. The logo, palette and PIXAPoLY wallpaper come from the PolyOS 7 Scratch project
(CC BY-SA 2.0). Poppins is under the SIL OFL. Details are in [CREDITS.md](CREDITS.md).
