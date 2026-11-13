# Radiolack rewrite: scoping

Status: v0 merged into `main` and released as 0.2.0 on 2026-11-10 (started
on the `rewrite` branch on 2026-11-08), and accepted by the owner the same
day with one check left (see v0 below). v1, remote access, started on
2026-11-11. v2, the scanners, was designed with the owner on 2026-11-12.
Visual reference: [`ui-mockup.html`](ui-mockup.html), validated on 2026-11-08
(simulated data, no receiver, no audio).

This document fixes what the rewritten Radiolack is, what it is not, how it is
built and in which order. The first milestone replaced the Gqrx-driven airband
tool on `main`; that tool stays in the history under the tag `v0.1.0`.

## Purpose

Radiolack becomes a standalone, receive-only SDR program for every frequency
its receiver covers, with a ham-focused interface: a band plan strip names
what each range is used for, with detailed plans inside the amateur bands. It
reads I/Q samples from an SDR receiver, shows a full-screen spectrum and
waterfall, demodulates and plays the signal, and scans the bands for
activity. It runs as a desktop application and can let a phone, a tablet or
another computer control it and listen to it over the local network. It
depends on no other radio program, only on receiver drivers.

## Decisions

| Date | Decision |
| --- | --- |
| 2026-11-08 | Standalone program for the amateur bands, inspired by SDR++, SDR#, SDRangel and OpenWebRX+. No dependency on Gqrx or any other radio program; receiver drivers (rtl_tcp, librtlsdr, SoapySDR) are fine. |
| 2026-11-08 | Receive-only. Transmit stays out of scope until the owner holds an amateur licence, and would then need a decision of its own. |
| 2026-11-08 | Same repository, rewritten from scratch on the `rewrite` branch. The name "Radiolack" shows discreetly in the top-left corner of the UI. |
| 2026-11-08 | Spectrum and waterfall fill the screen; discreet buttons open panels for fine-tuning; many display options; peak scanner and sweep scanner. |
| 2026-11-08 | Desktop application. A Settings option turns on a built-in web service so a phone, a tablet or another computer can control it and receive its audio. |
| 2026-11-08 | Phones are first-class remote clients (touch, portrait and landscape). No receiver is ever plugged into a phone. |
| 2026-11-08 | Maximum compatibility: the owner uses an iPhone, but every mainstream browser and the three desktop systems are targets. |
| 2026-11-08 | Making the program available to the public is the long-term goal: distributing it so that anyone can install it at home. Opening a receiver to anyone on the internet (a public WebSDR) is not a goal. |
| 2026-11-08 | The UI mockup is the visual reference. It can still change deeply. |
| 2026-11-08 | Desktop shell for v0: the system's WebKitGTK through PyGObject, validated by spike 1. Windows and macOS shells are chosen before v4. |
| 2026-11-09 | UI code: plain JavaScript ES modules served as they are, with no build step. Types are written in JSDoc comments and checked by TypeScript; only that check needs Node. Revisit if the UI needs a framework or npm libraries. |
| 2026-11-09 | English only: code, identifiers, comments, documentation and the interface. No other language and no translation layer; this replaces the English and French interface planned on 2026-11-08. |
| 2026-11-09 | All frequencies: Radiolack tunes and labels the whole range of its receivers, not only the amateur bands. The band plan covers 9 kHz to 6.425 GHz with a simplified European table (ITU Region 1 allocations named by their main use) and simplified IARU Region 1 plans inside the amateur bands ([`band-plan.md`](band-plan.md)). This replaces "for the amateur bands" (2026-11-08) and answers the open question on tuning. |
| 2026-11-09 | Browser tests (Playwright: Chromium, Firefox and WebKit) run headless and only on request (`RADIOLACK_BROWSER_TESTS=1`), as CI does; the usual test commands launch no browser. |
| 2026-11-09 | Persistence: the engine saves everything, in `$XDG_CONFIG_HOME/radiolack` (`settings.json` and `bookmarks.json`, each with a format version), including the interface's own settings: the page's origin, and with it the browser's storage, changes with the port at each start. Receiver settings are saved per receiver (the simulated band, each `--rtl-sdr` device, each `--rtl-tcp` address). Until remote access, one set of interface settings is kept, that of the interface that changed one last, which the next interface opens with; open interfaces keep their own. v1 keeps them per device. Remote access is never saved. A file Radiolack cannot read is set aside, never overwritten. |
| 2026-11-09 | rtl_tcp address: the address is only the way to the receiver, and changes with the networks the computers are on. The user gives the address that works where they are, on the command line or in the Source panel, and Radiolack says when it does not answer; it does not try several addresses by itself. One set of rtl_tcp settings is kept whatever the address, with the address used last, which `--rtl-tcp` without an address reopens. This replaces "each `--rtl-tcp` address" in the persistence decision above. |
| 2026-11-10 | Source choice: the Source panel's Receiver list switches between the simulated band, a receiver plugged into this computer and an rtl_tcp server while Radiolack runs; each keeps its own settings. Without `--rtl-sdr` or `--rtl-tcp`, Radiolack opens the source used last, the simulated band the first time. |
| 2026-11-10 | Installation: each system gets its usual installer. The Debian package comes first, for Debian 12 or later and Ubuntu 24.04 or later, on the system's Python and libraries, with a menu entry and an icon; the owner's v0 check uses it. The macOS (.dmg) and Windows (.msi or .exe) installers come with their window shells, before v4. |
| 2026-11-10 | Updates: at start, Radiolack says when a newer version is out, with a button that installs it and restarts Radiolack; Settings checks on demand and can turn the check at start off. On Linux the update comes from a signed APT repository and upgrades Radiolack only, once the system has asked for the administrator's password. The button shows in the local window only, never through remote access. The check is the only connection Radiolack makes besides the receiver and its clients, and carries no identifier. It needs published packages: see the publication decision below. |
| 2026-11-10 | Licence: GPL-3.0-or-later, copyright OGD09. It is the licence of most SDR software (Gqrx, SDR++, SDRangel, GNU Radio), whose code Radiolack can then reuse; anyone who distributes a modified Radiolack must publish its source under the same licence. Every dependency fits it (aiohttp: Apache-2.0; NumPy: BSD; PyGObject, GTK and WebKitGTK: LGPL), and so do GPL drivers that installers may carry, such as librtlsdr. Set aside: the AGPL, OpenWebRX's choice, whose duties for versions used over a network Radiolack's private remote access does not call for, and permissive licences, which allow closed versions. |
| 2026-11-10 | Versions: three numbers, X.Y.Z, the middle one for new features and the last one for fixes. Each release is an annotated tag `vX.Y.Z` on `main`, made with `packaging/release.py`; between releases `main` holds the next development version, X.Y.Z.dev0, so that a package built from it installs over the release. The airband explorer is `v0.1.0`, its own version, and the rewrite starts at `v0.2.0`. A published tag never moves. |
| 2026-11-10 | Publication: the repository becomes public. Updates need packages that Radiolack can fetch without the owner's credentials, and the packages hold the Python code anyway. The history was audited first (2026-11-10) and stays as it is: rewriting it would not remove the commits GitHub keeps for pull requests. The owner switches the repository's visibility. |
| 2026-11-10 | Releases are built, checked and published by GitHub Actions from their tag, never from a computer, with security first: the tag must be annotated, on `main` and name the version its commit holds; the tests, the interface checks and the package's installation on each supported system run on that commit, without any cache; the package is reproducible and published with its checksum and, once the repository is public, an attestation of its build, by one job in a `release` environment whose protection rules the owner sets. Every action is pinned to a commit and updated by Dependabot a week after its release, and actionlint and zizmor check the workflows. The APT repository's signing key is decided with that repository. |
| 2026-11-10 | APT repository: the owner signs it on their computer, with the signing subkey of an Ed25519 key whose primary key stays offline, in the owner's password manager, with its revocation certificate; another computer of the owner's may get a signing subkey of its own later. One suite, `stable`, and one component, `main`, with architecture-independent packages; the indexes also stay under their checksum (Acquire-By-Hash), so that a web cache never pairs an old index with a new Release file. `packaging/apt_repository.py` signs only what the key signed before and the packages it is given, once gpgv has checked the repository's signature: a published version never changes. No `Valid-Until`: apt would refuse the repository whenever the owner did not sign it again in time. Without it, someone who could change the published site could serve an old index, which apt would accept; HTTPS keeps others on the network from doing it. apt refuses a signature by an expired key: the signing subkey's expiry is extended, and a release carries the new public key, well before it expires (the tests fail a year before). The package adds the repository's source and key as configuration files in /etc/apt, the key in /etc/apt/keyrings rather than /usr/share/keyrings: a removed but not purged package keeps its source, and apt update would fail without the key. GitHub Pages serves the `gh-pages` branch, which `packaging/publish_apt.py` updates only with a release that its tag rebuilds, byte for byte, on the owner's computer, and pushes only once the owner agrees. |
| 2026-11-10 | Updates, first part: the owner chose the alert before the button. Radiolack says when a newer version is out and sends the user to the system's software updates, which install it from the APT repository; the button that installs it, with the administrator's password, comes later if still wanted. The check needs no privilege: it runs apt as the user, with Radiolack's APT source alone and none of the system's apt configuration (other repositories, proxies, hooks), in a temporary folder, so that it never touches the system's package lists, and compares the repository's latest version with the installed package's. Only a Radiolack that runs from the Debian package checks. A check that fails stays in Settings: the notice shows only a newer version, or one installed since the start, which runs once Radiolack restarts. |
| 2026-11-11 | Remote access, answering spike 3: Radiolack makes its own local certificate authority, which each device installs once, after checking its fingerprint against Settings. Safari's WebSocket refuses a certificate that the device does not trust, even after the user accepts the warning, so plain self-signed certificates would not do. The root's key signs one intermediate, which name constraints limit to private IPv4 addresses and to names ending in `.local`, `.home.arpa` and `.internal`, then is destroyed: even stolen, the authority can vouch for no Internet site. The intermediate signs a new server certificate at each turn-on. Set aside: Tailscale certificates (a third-party account, an app on each device, names published in Certificate Transparency logs, the VPN even at home) and a domain with Let's Encrypt (a yearly cost, DNS credentials on the computer, routers that block private DNS answers); the user's own certificate may come later. Remote access is off at each start, and only this computer's interfaces turn it on; it listens on port 8443 by default, on this computer's private IPv4 addresses only, for peers on those networks, and never asks for a port forward or touches the firewall. A device pairs with an 8-character code (40 bits), answered after a second when wrong; 10 wrong codes block it until a new code is made, which signs every device out. A paired device keeps a `__Host-` session cookie, of which the computer keeps only a hash, until it is forgotten in Settings or unused for 90 days. Paired devices control the radio, sources and bookmarks included, but not remote access, the update check, the rtl_tcp server's address or where the settings are saved. `remote.json` keeps the port, the code, the paired devices and the authority, which replaces "Remote access is never saved" (2026-11-09): whether it is on is never saved. A key for the other accounts of a shared computer, which reach 127.0.0.1 as it is, waits for v4. Order: remote access, then listening on the iPhone (touch gesture, audio session, wake lock), then interface settings per device. |
| 2026-11-12 | v1 goes on with remote use from outside the home, then interface settings per device; this replaces the order of 2026-11-11. Listening on the iPhone ends without a wake lock: with Safari's playback audio session, the iPhone keeps playing with its screen locked (spike 2). Away from home, Radiolack runs on a computer next to the receiver, without a window, and devices reach it through the user's VPN: rtl_tcp's samples (16 Mbit/s at 1.024 MS/s) are too much for most links. A device's stream carries 2.2 Mbit/s: 2 Mbit/s of spectrum (30 frames per second) and 0.2 Mbit/s of PCM audio, so remote devices first get a lighter spectrum, then Opus audio. How remote access turns on without a window is an open question. |
| 2026-11-12 | Lighter stream for remote devices, as the owner chose: about 15 spectrum frames per second, each the mean power of two, in at most 2048 bins, each the highest of the bins it groups, so that narrow signals stay visible, and deflate (RFC 7692) on remote access's stream; this computer's window keeps every frame and every bin. Measured on the simulated band, a device's spectrum drops from 2 Mbit/s to 0.12 Mbit/s, about 0.3 Mbit/s with the PCM audio. A device's finest zoom becomes 16 of its bins, 18.75 kHz at 2.4 MS/s instead of 4.7 kHz. Set aside for now: a device that sends its view and gets only the visible bins, which keeps the detail but loses the waterfall's history at each zoom. When frames come slower than its lines, the waterfall repeats the latest frame, so that it keeps its speed. A remote stream keeps at most 16 KiB unsent, in its socket and its transport: over a link slower than the stream, the system's send queue otherwise grew without end and the delay with it (15.6 s after 60 s at 250 kbit/s), whereas the engine now drops old frames and the delay stays near 5 s at that rate, held by the TLS layer's buffers; Opus audio should leave them empty on such links. |
| 2026-11-12 | Remote access without a window, answering open question 3: the owner chose an option at start. `--remote [PORT]` turns remote access on at start, as Settings does, on PORT or the port used last, which it keeps, so that a computer without a window chooses its port too. Radiolack prints the addresses, the authority's fingerprint and the access code, as the owner agreed: a computer without a display has nowhere else to show them, and the code then lands wherever the output goes, a service's journal for instance. When remote access cannot turn on, without an address on a local network yet or on a port in use, Radiolack exits with status 1, so that a service manager starts it again. Only this computer turns remote access on, by its interface or by this option, which replaces "only this computer's interfaces turn it on" (2026-11-11); whether it is on is still never saved, and no setting turns it on. Settings stays on 127.0.0.1, which an SSH tunnel reaches. Set aside: Settings as the only way, through an SSH tunnel after each start, which a computer that restarts alone could not follow. |
| 2026-11-12 | Updates, no install button: the owner keeps the system's software updates, or apt in a terminal, which install a newer Radiolack well enough, and drops the button of 2026-11-10 that would install it with the administrator's password and restart Radiolack. The alert at start stays as it is, and Radiolack installs nothing itself. |
| 2026-11-12 | Opus leaves v1, as the owner chose: with the lighter stream, a device needs about 0.3 Mbit/s with the PCM audio, which the home Wi-Fi and a VPN carry, so Opus audio comes only if a mobile link shows dropouts. This replaces "then Opus audio" in the first decision of 2026-11-12. |
| 2026-11-12 | Interface settings per device, as the owner validated: each paired device keeps every preference that its interface gives (the display, the palette, the volume, the tuning step, the band plan's choice of mode, the scanners' settings), starting from this computer's, which it follows until it changes one. This computer's interfaces, which cannot be told apart, keep their one set, that of the interface that changed one last. A device's set goes with it, forgotten in Settings, unused for 90 days or signed out by a new code: paired again, it starts from this computer's. The sets are kept in `remote.json`, in each device's record, so that a device and its settings come and go together; `settings.json` keeps this computer's, today's single set. A set has the limits of today's (100 values), for at most 20 devices. Neither the files' format version nor the protocol's changes: an older Radiolack reads the devices without their settings, which it drops at its next write, and a new protocol version would stop the interface that a running Radiolack reads from the disk at each request, from the installation of a newer package until Radiolack restarts. Set aside: the browser's storage on each device, which Safari erases after seven days of use without a visit to the site and private browsing never keeps, and one set shared with the devices, whose volume and layout would overwrite the window's. The scanners' settings may become the engine's, shared by every interface, when v2 moves the scanners into the engine. |
| 2026-11-12 | Scanners, as the owner validated: the engine runs them for every interface, and the interface shows and drives them. One detector finds the signals at least a threshold above the local noise, with the airband explorer's method ([`lessons.md`](lessons.md): the median of the nearby bins, the receiver's edges and its centre left out, two detections in a row), in the FFTs that the engine computes anyway, averaged over about a third of a second, at full resolution whatever the device. One list of the signals found, with Tune and Ignore, shows on every interface and marks the spectrum. The memory scan watches every ticked bookmark of the receiver's span at once, from the power of each one's channel in the same FFTs, jumps to the strongest active one, lets the squelch confirm, and goes on once the channel has stayed silent for the resume time: there is no dwell per channel. The sweep steps the receiver across a range in overlapping windows ([`lessons.md`](lessons.md)), with 0.4 s of settling then the time per window at each, pass after pass until stopped, then brings the receiver and the VFO back where they were; its signals join the same list. It needs a source that retunes, an RTL-SDR on this computer or behind an rtl_tcp server whose link keeps up with the receiver: at 2.4 MS/s and 0.5 s per window, a pass takes about 15 s across 70 cm, 30 s across the airband and 1.5 min across 23 cm. The scanners' settings become the engine's, shared and saved with the settings: detection on or off, the threshold, the resume time, the time per window, the sweep's range and up to 200 ignored frequencies, kept across restarts. This replaces "the scanners' settings" among each device's preferences (2026-11-12): only "Mark on the spectrum" and the tab shown stay each interface's own, and the scanner preferences saved so far are dropped. Set aside: detection in each interface, which gives each device its own list, coarser bins on a phone (2048) and no history; a dwell per channel, which takes 2.5 s to go round ten bookmarks at 0.25 s each and misses short bursts; a threshold of the sweep's own; and a panorama of the swept range, which may come later: while it sweeps, the spectrum shows each window as it passes. |
| 2026-11-12 | Scanners and the one receiver: detection follows the receiver and changes nothing. One scan runs at a time, the memory scan or the sweep, and starting one stops the other. Tuning by hand from any interface (the VFO, the centre frequency, the sample rate or the source) stops it, as the interface's memory scan did; the mode, the filter, the squelch and the gain do not. Every interface may start and stop a scan, paired devices included, as they control the rest of the radio (2026-11-11). A running scan keeps the engine reading, and the receiver open, without any interface, until it is stopped: started from an iPhone, it goes on with the iPhone locked. Whether a scan runs is never saved, so none runs at start, as with remote access. Detection alone keeps nothing open: it runs while the engine reads for an interface or for a scan. Every interface's status line names the scan that runs. |
| 2026-11-12 | Observations, as the owner validated: the engine keeps what it detects in `observations.json`, in the settings folder, written by the same store as the other files (whole-file writes, readable by the user only, an unreadable file set aside, its problems in Settings > Storage), at most every 10 minutes and when Radiolack stops. It holds one entry per channel, detections within 1.5 kHz of each other merged: when it was first and last detected, how many transmissions, how long it was active, its highest margin and 24 counters by hour of the day. At most 2000 channels, the least recently detected going first, and a channel undetected for 90 days is forgotten: about 0.6 MB at most. Neither the simulated band nor the ignored frequencies are recorded, and nothing but frequencies, times and levels is kept: no audio and no samples. A History tab lists the channels, with Tune, Bookmark and Clear. The observations stay apart from the airband explorer's history: `data/` is neither read nor imported. Set aside: a journal of each transmission (JSON Lines or SQLite, several MB), which may come if the counters fall short, and `$XDG_STATE_HOME`, the XDG folder for a history, which would be a second folder to document, protect and keep apart in the tests. |
| 2026-11-12 | Scanners in the protocol: a `scanner` state (the settings, the scan running, its progress and why it stopped), a `peaks` message (at most 50 recent signals, at most once a second and only when they change), the observations, sent only to an interface that asks for them, and `scanner` and `observations` messages from the interfaces. The protocol stays at version 6, and the hello lists the scanners that the engine runs: a newer page on an older engine greys out only what that engine lacks, and an older page ignores the new messages. Version 7 would stop every page loaded between a package's installation and Radiolack's restart, since the server reads the interface from the disk at each request: a device facing a computer without a window could then neither use Radiolack nor restart it. Serving the interface as it was read at start would remove that case for every later version; it does not depend on v2 and is left for later. |
| 2026-11-12 | v2's budget and order: the scanners add at most 2% of one core on the owner's laptop (spike 4); a scan without any interface costs what the engine costs for one, 12 to 15% of one core, and rtl_tcp, while it runs. Pull requests, in this order, each with its documentation and tests: this design; detection and its list in the engine, with the shared settings and the ignored frequencies, in place of the interface's own detection; the memory scan in the engine, in place of the interface's; the observations and the History tab; the sweep, with which the memory scan also visits the bookmarks outside the receiver's span. The tests need no receiver: detection and the memory scan run on the simulated band, the sweep against the fake rtl_tcp, which gains carriers at fixed frequencies. |
| 2026-11-12 | History and paired devices, as the owner chose: every interface may clear the History, a paired device's included, as it changes the bookmarks; Clear and Clear all wait for a second press on every device. With the observations' pull request, a channel gathers the signals found within 1.5 kHz of it or within a quarter of their width, as the list of the signals found does, so that a wide FM signal whose centre of power moves with the voice stays one channel. |
| 2026-11-12 | The sweep, as the owner chose, comes alone in its pull request; the memory scan's visits to the bookmarks outside the receiver's span come in the next. Its windows lie a quarter of the sample rate apart at most, the first and the last a tenth of the rate inside the range, and a narrower range gets two: every frequency of the range then lies in the part that detection searches of a window, but a signal at a window's centre only in its neighbours'. So the time per window holds two searches of detection, each averaging half of it, and a signal is confirmed, two detections in a row, within one window, without waiting for the settling of the next. A window's time counts from its first samples, which the source labels with the settings they were taken with; the sweep moves on when, 2 s after asking for a window, the samples still come from elsewhere, as when the tuner keeps the frequency within its range. A client's stop, or a window that the receiver refuses, brings the receiver and the VFO back; tuning by hand and another source leave them where they are, and while the sweep runs, the receiver's saved settings are those it comes back to, so that a restart meanwhile finds the receiver where the user left it. The scanner state names the scan that its stop reason is about, so that each tab shows only its own, and while the sweep runs, the memory scan's bookmarks are those of the span it comes back to, which a memory scan started then watches. Set aside: the sweep on the simulated band, which does not tune. |
| 2026-11-12 | The memory scan's visits to the ticked bookmarks outside the receiver's span, as the owner validated: only with a receiver that tunes, and only when a ticked bookmark within its reach lies outside the span; otherwise nothing changes, and with the simulated band the scan watches the span's alone. The bookmarks within reach take as few windows as the engine finds, each in the part of a window that detection searches, the span's included, and the scan visits them in turn, in frequency order from the window nearest the receiver, the lowest after the highest. It watches each window for a fixed 1 s once the noise there is known, and again after each channel it stopped on, with no setting of its own; a window whose samples do not come within 2 s is left, as with the sweep. Each visit leaves the bookmarks unwatched for about 0.75 s, the receiver's 0.4 s of settling and the noise's 0.33 s: with N windows, a transmission shorter than about 1.75 × N − 1 s may be missed, 2.5 s with two windows. A stop while watching brings the receiver and the VFO back where the scan found them, once it moved the receiver; on a channel they stay, so that a stop keeps what is heard; tuning by hand and another source leave them where they are. While either scan runs, the receiver's saved settings are those of its start. The protocol stays at version 6: `memory.watched` lists every bookmark the scan visits, the scanner state gains the number of windows and the window visited, and the list marks the ticked bookmarks beyond the receiver's reach. Set aside: a time per window for the memory scan, and windows centred on their bookmarks rather than starting from the lowest, which may come later. |
| 2026-11-13 | Navigation past the span, as the owner chose after a study of the conventions of eight SDR programs, intuitiveness first. With a receiver that tunes, the view goes past the span, out to ten spans wide with its centre within the receiver's reach; zooming out holds at the span's width before going past it, and the simulated band keeps the view within its span. Nothing is measured outside the span, and nothing there may look like a measurement: the span stands as a lit window whose light fades away from its edges into a darker background, where the grid goes on, blurred and fading too, and the band plan's colours rise as a faint glow from the strip; nothing stands where the trace would be, and the waterfall stays plain; beyond the receiver's reach, hatching. A first version held the trace's place with a flat blurred line at the span's noise level; after a try, the owner chose the fading light from captures of three other decors (nothing, the light, the trace dissolving within 40 px of the edge), as a flat line also reads as a quiet band. Every way of tuning outside the span follows one rule: the span moves so that the frequency sits a 32nd of the span above its centre (75 kHz at 2.4 MS/s), with the channel at least 5 kHz from the centre, clear of the DC residue; this replaces the quarter of the span used so far, so that the new span shows about as much on each side of the frequency. A click outside the span tunes there, beyond the receiver's reach to the nearest frequency. Before it, a mouse sees an outline of where the span would go and a tip, which warns when the click stops a scan; after it, the frequency shows at once, a dashed outline marks the span on its way and the old span's spectrum dims until frames come. A view that showed the whole span shows the new one; another stays where it is while it shows the VFO. A label at the spectrum's edge gives the VFO's frequency when it lies outside the view, and brings the view back to it. While the view does not frame the span, a small button with an icon (two bars, an arrow between them) at the bottom right of the spectrum frames it again, as the 0 key does; the owner asked for an icon rather than a word after a first try. The band strip shows a band's ranges while they average 12 px, else the whole band as one block, named in full, by a new optional `short` name or not at all, which a click frames. Pull requests, in this order: this one; moving the span (the jump at the edge for the wheel, the arrow keys and the step buttons, Shift and drag to move the span, Shift and click, Page Up and Page Down); the waterfall realigned when the span moves, with a blurred memory of the spectrum last seen outside the span if the owner wants it. Set aside: any activity shown outside the span that was not measured there (a measuring display must not invent signals), bumps or a noise texture in the decor, which would imitate a measurement, and History marks outside the span. |
| 2026-11-13 | Moving the span, as the owner narrowed it after trying the first pull request: dragging the view, which never moves the VFO, and a click outside the span cover the rest. A step of the wheel, the arrow keys or the − and + buttons that would enter the outer tenth of the span, which the receiver's filter dims, moves the span by the rule above, as SDR++ does in its normal tuning mode: about every megahertz at 2.4 MS/s. One move at a time: steps made while the receiver settles add up from where the VFO shows, and apply once frames come from the new span, so that a fast wheel or a held button never sends a burst of changes. The simulated band's VFO still stops at its edge. The realigned waterfall comes forward into the same pull request, since each jump would otherwise clear it: when the same receiver's span moves by less than its width, at the same sample rate and FFT size, each line keeps its frequencies and shows within the span only, what left the span going and the new part starting empty; the peak hold moves the same way. A longer move, another rate or FFT size, or another receiver starts them again, and the sweep's overlapping windows now leave their lines in the waterfall. Kept in reserve: Shift and drag to move the span, Shift and click, and Page Up and Page Down. The blurred memory stays the owner's choice. |

## Scope

In scope, across the milestones below:

- Receivers: RTL-SDR through rtl_tcp first, then SoapySDR devices.
- Frequencies: the whole range of the receiver; the band plan covers 9 kHz to
  6.425 GHz.
- Display: spectrum and waterfall with zoom and pan, past the receiver's span
  with a receiver that tunes, palettes, averaging, peak hold, a band plan
  strip and bookmarks on the spectrum.
- Reception: one VFO (NFM, AM, USB, LSB, CW) with filter width, squelch and
  AGC. More VFOs only if a later need shows up.
- Scanners: peak detection, sweep across several receiver windows, memory scan
  over bookmarks.
- Band plans: Europe first (ITU Region 1, with the IARU Region 1 amateur
  plans), then the other regions.
- Remote access: control and audio over the local network.
- Language: English only.

Out of scope:

- Transmitting, in any form.
- Recording audio, transcriptions or raw I/Q (current repository rule; see the
  open questions).
- Receivers attached to phones, and native mobile apps.
- Opening a receiver to anyone on the internet (a public WebSDR).
- Driving other radio programs.

Dropped from the current code: the VHF airband explorer and its Gqrx bridge.
Their migrated history in `data/` stays untouched; it is never written,
replaced or deleted. What the new engine reuses from that code is summarised
in [`lessons.md`](lessons.md).

## Compatibility targets

| Area | Targets |
| --- | --- |
| Desktop app | Linux x86_64 first. Windows 10/11 and macOS (Apple silicon and Intel) for the public release. Proposed later: a headless mode on Linux arm64 (Raspberry Pi next to the antenna), used only through the web UI. |
| Remote clients | Safari on iOS and iPadOS, Chrome, Edge and Firefox on desktop and Android, Safari on macOS: current and previous major versions. |
| Receivers | RTL-SDR (R820T and R828D tuners, RTL-SDR Blog V3 and V4) first, then SoapySDR devices (Airspy, HackRF, SDRplay, LimeSDR, PlutoSDR). |
| Language | English only, whatever the system language. |

## Architecture

```mermaid
flowchart LR
  subgraph app ["Radiolack desktop app"]
    src["Source: rtl_tcp, SoapySDR"] --> dsp["DSP: FFT, channel, demodulators"]
    dsp --> scan["Scanners"]
    dsp --> srv["Web server: HTTP and WebSocket"]
    scan --> srv
    srv --- store[("Settings, bookmarks, observations")]
    srv --- win["Native window: web UI"]
  end
  rx[("SDR receiver")] --> src
  srv -.-|"LAN, HTTPS, access code, only when enabled"| remote["Phone, tablet or other computer: same web UI"]
```

### Components

- **Engine** (Python 3.11+, asyncio, NumPy). It owns the receiver and all
  state.
  - Sources: an rtl_tcp client, with an rtl_tcp child process that Radiolack
    starts on a free local port; SoapySDR later; synthetic I/Q generated in
    memory for tests and demos (never recorded). The Source panel switches
    between them while Radiolack runs.
  - DSP: display FFT (window, averaging, peak hold), channel extraction (mix
    and decimate), NFM, AM, USB, LSB and CW demodulators, squelch, AGC, audio
    resampling.
  - Scanners: peaks, sweep, memory.
  - Storage: settings, bookmarks, band plans and observations in the
    platform's directories (XDG on Linux, AppData on Windows, Application
    Support on macOS).
  - Server: aiohttp, for HTTP and WebSocket in one dependency. It serves the
    UI files and, per client, spectrum rows (uint8 dB values), audio, state
    updates (JSON) and commands.
- **UI** (JavaScript ES modules, served as they are: no build step). Types
  live in JSDoc comments and TypeScript checks them without producing files;
  nothing else needs Node. No UI framework at first, like the mockup; revisit
  if the panels grow complex. WebGL2 spectrum and waterfall, AudioWorklet
  playback behind a jitter buffer, mouse, keyboard and touch input. The
  files ship inside the Python package as they are.
- **Desktop shell**: a native window that shows the UI served by the local
  engine on the loopback interface. On Linux it is the system's WebKitGTK
  through PyGObject (spike 1). Windows and macOS shells are chosen before v4,
  with pywebview (WebView2, WKWebView) as the candidate. Where no native
  window is available, `radiolack --ui browser` opens the UI in the default
  browser.
- **Packaging**: on Linux, a Debian package (`packaging/build_deb.py`) that
  installs the Python package for the system's Python 3, with the
  `radiolack` command and its manual page, a menu entry, an icon, its
  changelog and its licence notice, and depends on the system's aiohttp,
  NumPy, PyGObject, GTK 3 and WebKitGTK. Its build is reproducible, and
  GitHub Actions publishes it with each release (`release.yml`). The macOS
  and Windows installers come with their shells (spike 5).

### Behaviour rules

- One receiver state is shared by every client: frequency, mode, filter,
  squelch, gain and scanners. A change from any client reaches the others at
  once, and the last change wins.
- View settings stay on each device: zoom, palette, panel layout, volume and
  mute. The engine keeps the latest for the next interface to open: one set
  for this computer's interfaces, and one for each paired device, which
  starts from this computer's (decided on 2026-11-12).
- The engine sends audio only to clients that turned it on, and encodes each
  format once whatever the number of clients.
- Settings live in the engine. Security settings (remote access, access code)
  can only be changed from this computer's interfaces: the window, or a
  browser on 127.0.0.1.

### Remote access

- Off by default. When turned on, the engine also listens on the local
  network, over HTTPS only.
- The app generates an access code (no password to invent). A device enters
  it once and keeps a session; "New code" signs every device out. Failed
  attempts are rate-limited.
- Settings list the paired devices, show which are connected, and forget one
  on request.
- From outside the home: through a VPN (Tailscale, WireGuard). Radiolack never
  asks the user to forward a port on their router.
- Remote clients need HTTPS anyway: browsers allow AudioWorklet, the wake lock
  and app installation only on secure pages. Radiolack's own local
  certificate authority, installed once per device, provides it (spike 3,
  decided on 2026-11-11).

### Audio path

- v0: the engine sends 16-bit PCM over WebSocket to the local window, which
  plays it with an AudioWorklet. The local window uses the same path as remote
  clients, so v1 only adds compression and the network hop.
- v1: PCM (192 kbit/s for 12 kHz mono), on the local network and through a
  VPN, where a device's whole stream needs about 0.3 Mbit/s. Opus, at about
  32 kbit/s, decoded by WebCodecs or by a WebAssembly decoder where
  WebCodecs lacks Opus, comes only if a mobile link shows dropouts (decided
  on 2026-11-12).
- iPhone with the screen locked: Safari may interrupt Web Audio when the
  screen locks or the page goes to the background. With Safari's "playback"
  audio session (`navigator.audioSession`), which iOS also plays with the
  ringer switch on silent, the owner's iPhone kept playing with the screen
  locked (spike 2, 2026-11-12), so a plain audio stream played by an
  `<audio>` element, which iOS keeps playing at the cost of a few seconds of
  delay, is not needed.

### Privacy

- No telemetry and no account. Radiolack talks only to the receiver, to the
  clients the user lets in and, for its update check, which Settings can
  turn off, to its APT repository.
- No browser data on disk: the window's WebKit context is ephemeral, and the
  engine saves what the interface needs with the settings.
- Nothing personal in the repository: no owner-specific frequencies, places or
  call signs in code, examples or tests.

## Milestones

### v0: local desktop app (Linux)

Goal: replace Gqrx for the owner's daily listening on 2 m and 70 cm.

- Engine and native window; rtl_tcp source; sample rate, centre frequency,
  gain, PPM correction and DC removal.
- Spectrum and waterfall with the mockup's interactions (mouse, keyboard and
  touch).
- One VFO: NFM, AM, USB, LSB and CW, filter width, squelch, audio on the
  computer.
- Bookmarks and the band plan: all frequencies, with the IARU Region 1 plans
  inside the amateur bands.
- Settings panel with persistence; English interface.
- Debian package with a menu entry and an icon.
- Tests without hardware: DSP on synthetic I/Q; engine and server over local
  sockets on ephemeral ports; UI tests in Chromium, Firefox and WebKit
  (Playwright); CI updated with job timeouts and cancellation.

Done when the owner confirms on their computer that Radiolack, installed from
its Debian package, starts from its menu entry, shows the live band, tunes a
local repeater with clean audio and remembers its settings, and when every
test passes in CI.

Accepted on 2026-11-10, with one check left. Installed from the Debian
package of v0.2.0, Radiolack started from its menu entry, showed the live
band from an RTL-SDR behind an rtl_tcp server on the local network,
remembered its settings across a restart and played voices in AM on the
airband; every test passed in CI. No analog voice was on the air in NFM:
the only active repeater carried digital voice (4FSK at 4800 symbols per
second), which the NFM demodulator rendered faithfully, its audio reaching
full scale, but which Radiolack does not decode. Left to check: an analog
NFM voice, clean and without saturation on loud passages; a fault found
there gets a fix release.

### v1: remote access

- Settings > Remote access as in the mockup: HTTPS, access code, connected
  devices.
- Remote clients: the same UI, touch input, shared receiver, a volume per
  device; PCM audio, and Opus only if a mobile link shows dropouts (decided
  on 2026-11-12). The engine keeps each device's interface settings apart
  (v0 keeps one set).
- iPhone: listening with the screen on and with the screen locked (spike 2:
  Safari's playback audio session).
- Use from outside the home (decided on 2026-11-12): Radiolack runs next to
  the receiver without a window, `--remote` turns remote access on there,
  devices reach it through the user's VPN, and remote devices get a lighter
  spectrum.

Done when, from the owner's iPhone on the home Wi-Fi, the owner can tune and
listen with less than half a second of delay, and no device connects without
the code.

### v2: scanners

Designed with the owner on 2026-11-12 (see Decisions).

- Detection in the engine: one list of signals for every interface, with
  Tune and Ignore, and marks on the spectrum.
- Memory scan in the engine: every ticked bookmark of the receiver's span
  watched at once, a stop on the active one, then on again after the resume
  time; with a receiver that tunes, the bookmarks outside the span too,
  window after window.
- Sweep across the receiver windows of a range, with an RTL-SDR.
- Observations kept across restarts, in a History tab, apart from the
  airband explorer's history.
- Scans started and stopped from any interface, paired devices included,
  which go on without any interface.

Done when, on the owner's computer with the receiver, every interface shows
the same signals, a memory scan started from the iPhone stops on an active
bookmark and goes on with the iPhone locked, a sweep lists the channels
active across a band and brings the receiver back, the History tab keeps
what was detected across a restart, and every test passes in CI.

### Later milestones

- v3, receivers and bands: SoapySDR devices, HF (direct sampling, RTL-SDR Blog
  V4), band plans for the other regions.
- v4, public release: Windows and macOS builds and installers, code signing,
  updates on every system, user documentation, accessibility review.

## Spikes and risks

| # | Question | Why it matters | When |
| --- | --- | --- | --- |
| 1 | Does the native window (WebKitGTK) draw a full-screen WebGL2 waterfall smoothly and play AudioWorklet audio at a low CPU cost on the owner's laptop? | It chooses the desktop shell. | Done: yes, within the limits below. |
| 2 | How does an iPhone keep listening with the screen locked? | The owner's phone; Safari interrupts Web Audio in the background. | Answered on 2026-11-12: with Safari's playback audio session, the owner's iPhone keeps playing with the screen locked. |
| 3 | Which HTTPS setup can a non-expert complete on an iPhone and on Android: a local certificate authority installed once per device, Tailscale certificates, or a certificate the user provides? | Remote clients need a secure page, and installing a certificate on iOS takes several steps. | Decided on 2026-11-11: a local authority limited by name constraints (see Decisions); to check on the owner's iPhone. |
| 4 | Python DSP load with scanners at 2.4 MS/s. | Measured on an i7-7500U: display FFT and one NFM channel take about 15% of one core, 23% with four channels. | Answered on 2026-11-12: the scanners need no channel of their own. Detection reads the FFTs that the engine computes anyway, 0.4 ms per averaged array about three times a second, and the memory scan reads its bookmarks' channels there, 0.08 ms per block for 200 bookmarks: together under 1% of one core, against 4.0 ms per block of 1/30 s, 12%, for the engine itself (its FFTs, one NFM channel and the audio). |
| 5 | Packaging Python, NumPy and drivers for Windows and macOS, including the WinUSB driver on Windows. | Public release. | Before v4 |

### Spike 1 result (2026-11-08)

A test page drew a WebGL2 waterfall (2048-bin rows, one per frame) under a
spectrum trace and ran a silent AudioWorklet, measured for 10 s after a 2.5 s
warm-up on the owner's laptop: i7-7500U, Intel HD 620 with Mesa 22.3.6,
Wayland, 59 Hz screen at scale 2. WebKitGTK 2.50.6 ran through PyGObject;
Chromium ran in app mode. CPU covers the whole process tree.

| Shell | Drawing size (device px) | Frames/s | 95th percentile frame | CPU (one core) | Audio |
| --- | --- | --- | --- | --- | --- |
| WebKitGTK, default policy | 3200 × 1662 | 52.2 | 26 ms | 57% | suspended |
| WebKitGTK, autoplay allowed | 3200 × 1662 | 50.6 | 28 ms | 82% | 48 kHz |
| Chromium | 2760 × 1628 | 57.2 | 19 ms | 84% | 48 kHz |
| WebKitGTK, autoplay allowed | 2400 × 1400 | 58.9 | 21 ms | 52% | 48 kHz |
| Chromium | 2360 × 1308 | 58.9 | 16.8 ms | 36% | 48 kHz |

- Both shells draw on the GPU. Up to about 3.4 million device pixels both
  keep up with the screen; at 5.3 million WebKitGTK drops to about 51
  frames/s while Chromium holds 57.
- At the same size Chromium uses less CPU (36% against 52% of one core).
- WebKitGTK keeps an AudioContext suspended until a user gesture, unless the
  web view is created with a policy that allows autoplay (WebKitGTK website
  policies, which can only be set when the view is created).

The mockup itself, served by the v0 skeleton in that window (1280 × 800 CSS
px, scale 2), runs at about 20 frames/s: its frame callback takes 40 ms, of
which 30 ms go to 2D-canvas `stroke()` calls for the spectrum traces and 7 ms
to `fill()`. In Chromium the same callback takes 2.5 ms.

Consequences:

- v0 uses WebKitGTK through PyGObject directly: system packages on Linux, no
  extra Python dependency, and control over options that must be set when the
  view is created, such as the autoplay policy.
- The UI draws the spectrum and the waterfall with WebGL2, not with 2D-canvas
  paths.
- The UI keeps its drawing buffers at about 3.4 million device pixels or less
  on large HiDPI windows, and starts audio on the first user gesture wherever
  autoplay is blocked, as remote browsers require anyway.

## Preparing a public release

Public distribution multiplies the work. These choices are cheap now and
expensive later, so they apply from v0:

- English only in code and interface: no translations to maintain.
- Band plans by region, with no owner-specific data.
- Cross-platform code paths only.
- Secure defaults for remote access.
- Dependencies whose licences fit Radiolack's, GPL-3.0-or-later.

These costs wait for v4:

- Code signing: the Apple Developer Program (99 USD a year) with
  notarisation, and a Windows signing certificate or signing service to avoid
  SmartScreen warnings.
- Installers and updates for three systems, built on each system (GitHub
  Actions minutes are free for public repositories).
- Support and documentation for other people's receivers and drivers.
- A check that the name "Radiolack" is free to use.

## Open questions

1. Recording: the repository forbids recording audio and raw I/Q today, while
   most SDR programs offer it. Keep the rule, or allow recording on demand?
2. Wide FM: the band plan shows FM broadcast (87.5 to 108 MHz), which needs a
   WFM demodulator, but the scope lists NFM, AM, USB, LSB and CW only. Add WFM
   to v0, or later?

## Repository plan

- `rewrite` branch created from `main` on 2026-11-08.
- v0 skeleton, 2026-11-08: the airband explorer's package, tests and docs are
  removed from the branch, after [`lessons.md`](lessons.md) kept what the new
  design needs. `radiolack` serves `src/radiolack/web/` (for now a copy of the
  mockup) on 127.0.0.1 and shows it in the native window, or in a browser.
  New `AGENTS.md` and `README.md` keep the safety rules; CI runs the new tests.
- Live display, 2026-11-09: the engine computes the display FFT (NumPy, 8192
  bins, about 30 frames/s) of a simulated 2 m band and streams it over a
  WebSocket ([`protocol.md`](protocol.md)) to the interface, which draws the
  spectrum and the waterfall with WebGL2 and keeps the mockup's panels and
  interactions. The interface is plain ES modules, type-checked from their
  JSDoc. Measured in the native window on the owner's laptop (1280 × 800 CSS
  px at scale 2; other programs were busy, so CPU figures vary by about
  ±10 points between runs): 56 to 60 frames/s against 20 for the mockup, a
  frame callback of about 1 ms, and 1.05 to 1.2 cores for the whole process
  tree while streaming: about 0.5 in the WebKit web process, 0.3 on the GTK
  main thread and 0.25 for the synthetic source and the FFT. With the stream
  open and no frames, the tree takes 0.08 to 0.13 cores; a static page takes
  0.01.
- Band plan, 2026-11-09: the strip, the readouts and the Bands panel read a
  band plan file served with the interface ([`band-plan.md`](band-plan.md)):
  198 bands from 9 kHz to 6.425 GHz, 81 segments inside them, checked by the
  tests and against the sources it names. The engine still captures the
  simulated 2 m band only.
- rtl_tcp source, 2026-11-09: `radiolack --rtl-tcp [ADDRESS]` reads an
  RTL-SDR receiver through an rtl_tcp server that the user runs. The source
  state (connection, tuner, settings) reaches every interface, which can set
  the centre frequency, the sample rate and the gain (protocol version 2); a
  typed frequency or a bookmark outside the span moves the span. A refused,
  silent or lost connection is a waiting state, retried by itself, and the
  source hangs up while no interface listens. Tested against a fake rtl_tcp
  server; not yet with a receiver.
- Managed rtl_tcp, 2026-11-09: `radiolack --rtl-sdr [DEVICE]` runs rtl_tcp
  itself, on 127.0.0.1 and a free port, for each connection, and stops it
  when the source hangs up or Radiolack stops, so that the receiver stays
  free for other programs while no interface listens; on Linux, setpriv has
  the kernel stop it should Radiolack die first. A receiver that rtl_tcp
  cannot open (none found, in use, no permission) is a waiting state with
  that reason. Tested with a fake rtl_tcp program; not yet with a receiver.
- Frequency correction and DC removal, 2026-11-09: the Source panel sets the
  receiver's correction in whole ppm, -100 to +100 (rtl_tcp command 5, sent
  at each connection like the other settings, with 0.4 s of settling after a
  change). DC removal is the engine's own setting, on by default and shared
  by every interface: it takes each block's mean off the samples before the
  FFT, which also removes a signal exactly on the centre frequency, and will
  apply to demodulation too. Tested with the fakes; not yet with a receiver.
- Type check and browser tests in CI, 2026-11-09: a second workflow, which
  runs only when the package, the tests or `pyproject.toml` change, checks
  the interface's types and runs Playwright tests in headless Chromium,
  Firefox and WebKit against the simulated band and the fake rtl_tcp
  program: the stream, the Source panel, and a change made in one interface
  showing in another. WebKit 27.2 refuses its own stylesheet for each
  `<select>` under the interface's policy, without visible effect
  (WebKitGTK 2.50 does not): the tests ignore that one message in WebKit.
  On GitHub's runner (Ubuntu 26.04, no GPU), Firefox first refused
  WebGL2: without a GPU it refuses Mesa's software renderer and needs a
  display, so the tests set its `webgl.force-enabled` preference and the
  workflow runs them under Xvfb, as checked locally with the GPU hidden.
- NFM and audio on the computer, 2026-11-09: the engine demodulates one VFO
  (protocol version 3). Overlap-save FFT filters shift the VFO to 0 Hz and
  bring the rate to 48 kHz, exactly for the usual receiver rates (2.4 MS/s
  by 1/50, 2.048 MS/s by 3/128), then keep the mode's channel, whose power
  each spectrum frame carries with the squelch's state. NFM goes through a
  discriminator, a 750 µs de-emphasis, a high-pass filter against CTCSS
  tones and a 3.5 kHz low-pass filter, to 16-bit PCM at 12 kHz for the
  interfaces that ask. The squelch, automatic (8 dB above the noise that the
  spectrum shows around the VFO) or set by hand, gates the discriminator's
  output with a 10 ms look-ahead and 5 ms fades: gating after the long audio
  filter let a burst of noise through at each close. The interface plays the
  audio with an AudioWorklet that keeps about 150 ms buffered, follows the
  engine's clock within 0.5% and resamples with a 16-tap windowed sinc
  (images below -89 dB). The VFO, mode, filter width and squelch are shared
  by every interface; the volume is each interface's own. In the native
  window the audio starts by itself; browsers start it on the first click or
  key press. On the owner's laptop (i7-7500U, other programs busy) the
  channel takes 1.8 ms per block of 1/30 s at 2.4 MS/s, 2.2 ms with the
  audio, about 6% of one core; the engine's process measured 38 to 44% of
  one core with the synthetic band, against 31 to 38% before. Raising
  glibc's mmap and trim thresholds made no measurable difference, so
  Radiolack leaves the allocator alone. Tested on synthetic I/Q, through the
  engine and the server, in three browsers and in the native window; not
  yet with a receiver. On GitHub's runner, which has no sound card or sound
  server, Firefox kept its audio context suspended, as it does locally with
  the sound server hidden: its audio library found no output channel, even
  with a null ALSA device. The workflow therefore starts PulseAudio with
  only a null sink.
- Settings and bookmarks, 2026-11-09: the engine restores at start, and
  saves a second after a change and when Radiolack stops, each receiver's
  source and demodulation settings, the bookmarks and the interface's own
  settings (protocol version 4: bookmarks, preferences and storage state).
  Files are written whole through a temporary file, synced, then renamed;
  writes run outside the event loop, and only for a file whose content
  changed. A file that is not valid JSON of a known version is renamed
  aside and Radiolack starts from the defaults; one that cannot be opened,
  or comes from a newer version, is left alone and not written until the
  next start; a failed write is tried again at the next change. These disk
  problems reach the interfaces apart from the receiver's state: Settings >
  Storage names them and the status line says when changes are not saved.
  A saved setting the receiver refuses keeps its default. The Bands panel
  adds, renames and removes the engine's bookmarks, which every interface
  shows. The tests run with a temporary settings folder.
- AM, SSB and CW audio, 2026-11-09: every mode plays; the protocol does not
  change, as `audio_modes` now lists all five. AM divides the envelope by
  the carrier's level, the higher of its averages over the 10 ms before and
  the 10 ms after each sample: the result is the modulation whatever the
  signal's strength, with no carrier left to thump at the squelch's gate,
  even where a carrier starts or stops at once (one average centred on the
  sample overshot by a third there). USB and LSB take the channel's real
  part, whose filter already keeps one sideband (the other comes out 88 dB
  lower); so does CW, the front filter putting the VFO at 700 Hz, so that a
  carrier on the VFO sounds at that pitch through a filter centred on the
  VFO. Their AGC follows the channel's power averaged over 10 ms, 10 ms
  ahead of the audio within the squelch's look-ahead, so that a sudden
  signal comes out without overshoot, and lets the gain come back at
  10 dB per second: a steady signal comes out at 0.4 of full scale from
  -30 to -80 dBFS. The AGC has this one speed; the Demod panel's AGC choice
  stays inactive. Through the synthetic band, the SSB contact plays in USB
  and vanishes in LSB, and the CW beacons sound at 700 Hz. Every mode costs
  about what NFM does: 3.6 to 4.0 ms per block of 1/30 s at 2.4 MS/s with
  the audio on the owner's laptop, CW the most, against 3.6 ms for NFM in
  the same run (other programs busy: the NFM entry measured 2.2 ms). Tested
  on synthetic I/Q and the synthetic band, and in three browsers; not yet
  with a receiver.
- rtl_tcp address in the Source panel, 2026-11-09: the address of an
  `rtl_tcp` source is one of its settings, which the Source panel's "Server
  address" field changes (settable in the source state; protocol version 4
  unchanged, as interfaces leave out the settings they do not know). A new
  address hangs up and connects there with the same settings, at once even
  while the source waits to try again; the tuner is unknown until the new
  server answers. The engine keeps one set of rtl_tcp settings with the
  address used last; an address given on the command line replaces the
  saved one, which is then written over at the next save or when Radiolack
  stops. Tested with fake rtl_tcp servers, and in three browsers against
  addresses where nothing listens. The panel's text fields show the engine's
  value once a change is sent, focus or not, and keep what the user types
  until then. Tried on 2026-11-09 with an R820T behind rtl_tcp on another
  computer, over a VPN with about 400 ms of round trip: the field moved the
  receiver to a port where nothing listens and back with the same settings.
  That link carried about 1.5 MB/s, short of the 2 MB/s of 1.024 MS/s: 16
  frames per second instead of 30, rtl_tcp dropping samples.
- Source switching, 2026-11-10: the Source panel's Receiver list switches
  between the simulated band, a receiver plugged into this computer and an
  rtl_tcp server, for every interface: the source state lists the `kinds`
  the engine can open, and a `source` message with `kind` alone switches
  (protocol version 4 unchanged). The engine keeps the settings of the
  source it leaves, closes it and opens the one used last of the chosen
  kind with its saved settings, demodulation included; it reads the new
  source only once the previous one has let the receiver go, as both may
  need the same device. `settings.json` records the source in use, which
  Radiolack opens at the next start unless `--rtl-sdr` or `--rtl-tcp` names
  one; a saved source it cannot open gives the simulated band. Tested with
  the fakes and in three browsers; not yet with a receiver.
- Debian package, 2026-11-10: `packaging/build_deb.py` builds an
  architecture-independent package with dpkg-deb alone, from the Python
  package as it is: the package in the system's `dist-packages`,
  byte-compiled at installation, the `radiolack` command, a menu entry and
  an icon. The window takes the program name `radiolack`, its application
  id on Wayland and its class on X11, so that the desktop matches it with
  the menu entry and the icon. A development version carries the time and
  commit of the last commit, and `.dirty` with uncommitted changes, so that
  a later build installs over an earlier one. A maintainer is required:
  dpkg warns about an installed package without one every time it reads its
  database. Tested by building, extracting and running the package; not yet
  installed.
- Licence, 2026-11-10: `LICENSE` holds the GPL's text as the Free Software
  Foundation publishes it (Debian's `/usr/share/common-licenses/GPL-3` is
  the same file), the README states it, and the Debian package carries the
  notice Debian expects in `/usr/share/doc/radiolack/copyright`. The Python
  package carries `LICENSE` too, built with Debian 12's setuptools (66) as
  with the latest (84), but `pyproject.toml` does not name the licence: its
  SPDX form needs setuptools 77, so Debian 12's would refuse the README's
  installation without build isolation, and the older table form is
  deprecated, with 18 February 2027 as setuptools' deadline.
- Leftovers, 2026-11-10: the window's WebKit context is ephemeral, so
  WebKitGTK no longer fills `~/.local/share/radiolack` and
  `~/.cache/radiolack` with a cache and storage that the interface does not
  use; checked in Xvfb with empty XDG folders, the page still loading. The
  programs the tests start that import the test package, such as the fake
  rtl_tcp, keep the run's settings folder instead of leaving one of their
  own in the temporary folder each time a signal stops them.
- v0 merged into `main`, 2026-11-10, with a merge commit that keeps the
  rewrite's commits as they are. The airband explorer stays in the history
  at commit `f0e1cf6`; work continues on `main`, and the `rewrite` branch is
  no longer used.
- Versions and tags, 2026-11-10: `packaging/release.py` makes a release on
  `main` (version commit, annotated tag, next development version) and
  pushes nothing; `tests/test_release.py` runs it against a local bare
  repository. Tags: `v0.1.0` on the airband explorer (`f0e1cf6`, whose
  version was 0.1.0) and `v0.2.0` on v0.
- v0 accepted, 2026-11-10: the owner installed the Debian package of
  v0.2.0 and checked v0 on their computer, with one check left, an analog
  NFM voice (see Milestones).
- Published packages, 2026-11-10: GitHub Actions builds the Debian package
  twice and compares the builds, byte for byte (every file carries the last
  commit's date), installs it with apt on Debian 12 and 13 and Ubuntu 24.04
  and 26.04, checks it there from the outside (`packaging/smoke_test.py`:
  127.0.0.1 only, Host and Origin refusals, interface and its
  Content-Security-Policy, stream, clean stop, and the window's toolkit
  under Xvfb), removes it and checks it with lintian. On a `vX.Y.Z` tag,
  `release.yml` checks the tag, runs every check on its commit without any
  cache, then publishes the package that the build made, with its checksum,
  on the tag's release page. The package gained a manual page and a
  changelog. Every action is pinned to a commit; Dependabot, actionlint and
  zizmor watch the workflows. Tested locally (two identical builds, the
  smoke test against the package's files, the release scripts against a
  fake GitHub command); not yet on GitHub.
- First published release, 2026-11-10: the repository is public, with the
  protections the owner set: the `release` environment, which only the
  owner approves; rules that keep `main` and the release tags from being
  deleted or rewritten; immutable releases; actions limited to GitHub's and
  zizmor's, pinned to commits; and a read-only workflow token. v0.2.1 is the
  first release that the Release workflow published. It carries an
  attestation of its build, and a rebuild from its tag on Debian 12 gives
  the same package, byte for byte.
- APT repository, first part, 2026-11-10: `packaging/apt_repository.py`
  builds and signs the repository (see Decisions); its tests sign with
  throwaway keys and read the repositories with apt-get run as a normal
  user, and the Package workflow installs the package on each system from a
  repository signed with a key made for the run. Tested with apt 2.6 and
  gpgv: a changed index, a changed Release file, another key and a changed
  package are all refused.
- APT repository, second part, 2026-11-10: the owner made the repository's
  key (an Ed25519 primary key kept offline, which only certifies, and a
  signing subkey that expires on 2029-11-09); the package adds the
  repository's source and key; `packaging/publish_apt.py` publishes a
  release on `gh-pages`. Tested locally: apt 2.6 reads the source, fails
  without its key (hence the key's place), and the command publishes only
  a release that its tag rebuilds, in a repository that the key signed.
- APT repository online, 2026-11-10: the owner published v0.2.1 with
  `publish_apt.py`, which started the `gh-pages` branch, and turned GitHub
  Pages on for it; then released v0.3.0, the first release that carries the
  repository's source and key, and published it there too. Checked: the
  served files are the branch's, the signatures check out with the key that
  the package carries, and apt-get over HTTPS lists both releases and
  downloads them as published.
- Update alert, 2026-11-10: at start, unless turned off, the engine checks
  the APT repository for a newer version (`updates.py`), and the interface
  says so at the top of the waterfall until closed; Settings > Updates shows
  the latest check's outcome, checks again and turns the check at start off.
  Protocol version 5. The tests run apt itself against repositories signed
  with throwaway keys, and the Package workflow checks the installed
  package's check against the published repository on each system.
- APT source, 2026-11-10: `apt update` noted at each run that the
  repository has no index for the computer's architectures (amd64, and
  i386 where it is added), as the repository lists only `all`. apt leaves
  such notes out when its output is not a terminal, which hid them from
  the checks. From 0.4.1, the source names `Architectures: all`, and a
  test runs apt with `quiet=0` against it; dpkg replaces the source at the
  upgrade, unless the user changed it.
- Remote access, first part, 2026-11-11: Settings > Remote access turns it on
  (`remote.py`, and a second HTTPS server in `server.py` that shares the
  engine), shows the addresses, the code, the paired devices and the
  authority's fingerprint, and a device that is not paired gets a pairing
  page, which offers the certificate and asks for the code. Protocol
  version 6. A tap starts the audio once the finger leaves the screen, as
  Safari on the iPhone requires (moved forward from the iPhone step). The
  tests check the certificates' chain and name constraints with OpenSSL,
  the refused peers, hosts, origins, codes, cookies and plain HTTP, the
  remote devices' rights, and turning on and off on 127.0.0.1; the browser
  tests pair a device and share the receiver with it; the Package workflow
  turns remote access on in each system's container. Checked on an iPhone
  on 2026-11-11, on a home Wi-Fi: the certificate, pairing, shared tuning
  and audio work by the computer's address. By its `.local` name, which
  Avahi also gives the computer's IPv6 address, where remote access does
  not listen, Safari opened nothing, so Settings gives the addresses first.
  The audio asks Safari for its "playback" session, which iOS plays with
  the ringer switch on silent; with it, the iPhone kept playing with its
  screen locked on 2026-11-12 (spike 2).
- Remote access, lighter stream, 2026-11-12: remote devices get half the
  spectrum frames, in at most 2048 bins (`engine.py`, `fewer_bins` in
  `spectrum.py`), over a deflated stream that keeps at most 16 KiB unsent
  (`server.py`); the waterfall repeats the latest frame to keep its speed
  (`main.js`). The tests check the remote frames' count, bins and mean
  power, the deflate negotiation and the inflated size limit, the send
  limits, and in the browsers the device's frame rate. A proxy that slows
  the link to 250 kbit/s, in a measurement outside the tests, showed the
  delay growing without end before (15.6 s after 60 s) and staying near
  5 s after. In headless browsers, software WebGL draws too few frames a
  second to measure the waterfall's speed; WebKit showed 22 to 24 lines a
  second from 12 to 13 frames a second.
- Held audio, 2026-11-12: through the VPN, the owner's iPhone stayed silent
  after each page load until a tap on the Demod button. Safari holds the
  audio until a gesture, and only the Demod panel said so, while phones
  hide the status line. As the owner asked, touch screens now show a
  speaker at the top right (under the menu buttons in landscape), crossed
  out while the page plays no sound (audio held, or volume 0) and with
  waves while it plays (`index.html`, `style.css`, `main.js`). A tap starts
  held audio, mutes by setting the volume to 0, as the Demod panel's slider
  would, so that the engine stops sending audio, or brings the volume back.
  It shows for touch input (`pointer: coarse`) rather than for iOS alone:
  other phone browsers may hold the audio too, and a mute within reach
  helps on any phone. Browser tests check it held and tapped, then muted
  (no more audio frames) and unmuted, with touch input in the three
  browsers, and that a page used with a mouse does not show it. Checked on
  the owner's iPhone on 2026-11-12.
- Remote access without a window, 2026-11-12: `radiolack --remote [PORT]`
  turns remote access on at start (`cli.py`, `LocalServer.turn_on_remote`
  in `server.py`), prints the addresses, the fingerprint and the code, and
  exits with status 1 when remote access cannot turn on. The tests run it
  on 127.0.0.1, with and without a port, and without an address or on a
  busy port; `smoke_test.py --remote` starts Radiolack with it and pairs a
  device with what it printed, in CI's containers or a network namespace
  of its own.
- Interface settings per device, 2026-11-12: a remote device's interface
  gives the engine its preferences too, which the engine keeps in the
  device's record in `remote.json` (`remote.py`, with their checks in
  `preferences.py`) and sends to that device's next interfaces; a device
  that gave none gets this computer's (`engine.py`, `server.py`,
  `main.js`). Protocol version 6 and the files' format version 1 stay. The
  tests check that a device starts from this computer's preferences, then
  keeps its own apart from this computer's and the other devices', across
  a restart, and loses them once forgotten, unused for 90 days or signed
  out by a new code, in the engine and through remote access's server; and
  in the three browsers, that a device's palette outlives a reload of its
  page while this computer's stays as it was.
- Detection in the engine, 2026-11-12, v2's first pull request:
  `scanners.py` finds the signals in the engine's spectra, at their full
  resolution, and keeps their list and the ignored frequencies; the engine
  sends every interface the scanner state and the list, at most once a
  second, and saves the settings with the others (`engine.py`,
  `server.py`). The Peaks tab, the spectrum's marks and Ignore show the
  engine's list (`stream.js`, `main.js`), and the interface's own detection
  is gone with its saved threshold. Levels below -150 dBFS count as
  -150 dBFS: rounding errors of samples without noise, such as the test
  source's, made a pattern that read as signals. Protocol version 6 stays,
  the hello listing `peaks`. The tests check the search on synthetic
  spectra, the list's rules, the engine's list for every client, remote
  ones included, the settings' checks, their saving and the rate of the
  list; checked by hand in a browser on the simulated band.
- Memory scan in the engine, 2026-11-12, v2's second pull request:
  `scanners.py` watches the ticked bookmarks of the span in the engine's
  spectra, each one's power in its mode's passband against the local noise
  there, and follows the scan's states; the engine tunes the VFO, with the
  bookmark's mode and that mode's filter width, for every client, keeps
  reading without any client while the scan runs, and stops it when an
  interface tunes by hand, changes the span or the source, or when no
  ticked bookmark is left in the span (`engine.py`). The scanner state
  carries the resume time, saved with the settings, the scan that runs, why
  the latest stopped and the memory scan's state, and the `scanner` message
  starts and stops it (`stream.js`). The Bookmarks tab drives the engine's
  scan and marks the ticked bookmarks outside the span, and the status
  line names the scan (`main.js`); the interface's own scan is gone, with
  its dwell and its saved resume time. Protocol version 6 stays, the hello
  listing `memory`. The tests check the channels' levels against the
  spectrum's calibration, the scan's states, the engine's scan for every
  client, its stops and its run without any client, and in the three
  browsers, two interfaces following a scan until one tunes by hand;
  checked by hand in a browser on the simulated band.
- Observations and the History tab, 2026-11-12, v2's third pull request:
  `observations.py` keeps the channels where the engine found signals,
  from the signals listed in each search, with the times, transmissions,
  active time, highest margin and hours of each, at most 2000 for 90 days;
  the engine records them, except on the simulated band, keeps them in
  `observations.json`, written at most every 10 minutes, after a clear and
  at stop, and answers an interface's `observations` message, a paired
  device's included, with them, after forgetting one channel or all if
  asked (`engine.py`, `server.py`, `store.py`). The History tab lists them
  with Tune, Bookmark and Clear, asks for them while it shows and confirms
  each clear with a second press, as `window.confirm()` would stop the
  page's audio (`stream.js`, `main.js`). Protocol version 6 stays, the hello
  listing `history`. The tests check the recording rules, the forgetting,
  the file's checks, the engine's recording, its refusals, the file's
  writing at most every interval and at stop, and in the three browsers,
  Tune, Bookmark and the confirmed clears from two interfaces; checked by
  hand in a browser on the simulated band with a prepared file, at a
  phone's width too.
- Sweep in the engine, 2026-11-12, v2's fourth pull request:
  `scanners.py` places the sweep's windows across a range and follows its
  progress, each window's two searches, the next window and the passes,
  leaving a window whose samples do not come; detection averages half the
  time per window for each search while it sweeps. The engine steps the
  receiver from window to window, for every client and without any client,
  brings the receiver and the VFO back when a client stops it or the
  receiver refuses a window, stops it when an interface tunes by hand or
  chooses another source, and saves the receiver's settings as they will
  be once back; one scan runs at a time (`engine.py`). The scanner state
  carries the range and the time per window, saved with the settings, the
  sweep's progress and the scan that the stop reason is about, and the
  `scanner` message changes them and starts the sweep (`stream.js`). The
  Sweep tab sets the range and the time per window, starts and stops the
  sweep and shows its pass and window, and the status line names it
  (`index.html`, `main.js`). A centre frequency or sample rate changed by
  hand now stops a scan at once, from the settings asked of the source
  rather than those of its samples, which on rtl_tcp came 0.4 s later and
  let the memory scan tune the VFO meanwhile. Protocol version 6 stays, the
  hello listing `sweep`. The tests check the windows and their coverage of
  the range, the sweep's steps, the engine's sweep with a fake receiver
  that tunes and with the fake rtl_tcp, whose carriers sit at fixed
  frequencies, its stops, returns and refusals, its run without any client,
  one scan at a time, and in the three browsers, two interfaces following a
  sweep of the fake rtl_tcp program until one stops it; checked by hand in
  a browser against the fake rtl_tcp program.
- Memory scan across windows, 2026-11-12, v2's fifth pull request:
  `scanners.py` places the windows that the ticked bookmarks within a
  receiver's reach take, as few as it finds, and follows the memory scan
  from window to window, leaving a window whose samples do not come. The
  engine plans the windows from the span where the scan started, and again
  when the ticked bookmarks or the receiver's range change; it asks the
  receiver for each window in turn while the scan watches, for every
  client and without any client, watches only the bookmarks of the span
  asked of the receiver, brings the receiver and the VFO back when the scan
  stops while watching once it moved the receiver, and saves the
  receiver's settings of the scan's start while either scan runs
  (`engine.py`). The scanner state's `memory.watched` lists every bookmark
  that the scan visits, with the number of windows and the window visited
  (`stream.js`); the Bookmarks tab shows the window and marks the ticked
  bookmarks beyond the receiver's reach (`index.html`, `main.js`).
  Protocol version 6 stays. The tests check the windows' placement and
  coverage, the visits' steps, the engine's visits with a fake receiver
  that tunes and with the fake rtl_tcp, its stops and returns, and in the
  three browsers, two interfaces following a memory scan of the fake
  rtl_tcp program across windows until one stops it; checked by hand in a
  browser against the fake rtl_tcp, with a carrier on a bookmark of
  another window: the scan stays on it, and a stop while it watches brings
  the receiver back; at a phone's width too.
- Next steps: with interface settings per device, v1's planned work is
  done, and Opus waits for a mobile link that shows dropouts (see
  Decisions, 2026-11-12). With the memory scan's visits to the ticked
  bookmarks outside the receiver's span, v2's planned pull requests are
  done too. Left: the owner's acceptance of v1, and of v2 with the
  receiver (see Milestones).
- `data/` stays untracked and untouched; `data/airband-peaks.json` is never
  written, replaced or deleted.
