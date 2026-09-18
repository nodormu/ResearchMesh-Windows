You're seeing this because `memories/01_environment_notes.md` and/or
`memories/01_system_tool_inventory.md` don't exist yet — this is either a fresh
install, or a fresh machine, and nobody has scanned it yet. `/memories` is the only
state that survives a session reset or restart — everything else (the Python kernel,
the browser page, the DuckDB connection) resets every time — so before anything else,
build that persistent memory now.

**Do this now, automatically — don't pause to ask first.** This is a standard,
safe, read-only inspection of the machine (checking OS version, hardware, installed
programs), not something that needs a confirmation step. The whole point is getting
straight into a working, self-aware session without extra friction.

**Figure out the real OS version first — don't assume.** Run `Get-ComputerInfo` (or
at minimum `[System.Environment]::OSVersion` plus `Get-CimInstance
Win32_OperatingSystem` if `Get-ComputerInfo` is slow/unavailable) to confirm the
actual Windows edition, version, and build number, rather than guessing from
context.

**Scan the real hardware**: CPU, RAM, GPU, disks — `Get-CimInstance
Win32_Processor`, `Win32_PhysicalMemory`, `Win32_VideoController`; disk layout via
`Get-Volume`/`Get-Disk`/`Get-Partition` (drive letters can be reassigned when
storage is added/removed, so don't treat a specific letter as permanent without
re-checking).

**Scan what's actually installed:**

- CLI tools on PATH via `Get-Command`.
- Installed programs via `winget list`, plus the registry (`HKLM:\SOFTWARE\
  Microsoft\Windows\CurrentVersion\Uninstall` and its `WOW6432Node` counterpart for
  32-bit entries) — `winget` doesn't track everything, especially older
  MSI/EXE-installed software, so both sources matter.
- GUI apps.
- Dev-assistant CLIs or reusable personal scripts you find lying around.

**Write exactly two files — but only whichever one is actually missing.** If one of
them already exists, leave it alone completely: don't regenerate or overwrite it just
because its sibling was missing. This fills a genuine gap, it never replaces existing
content.

- `01_environment_notes.md` — hardware specs, the Windows edition/version/build
  you actually found, disk layout, and any Windows-specific quirks or behaviors
  you hit along the way.
- `01_system_tool_inventory.md` — a categorized inventory of what's already
  installed (GUI apps, CLI tools, dev-assistant tools, reusable scripts) so you reach
  for something real instead of writing a one-off script from scratch every time.

**Write plainly, skip narration, keep the evidence.** State each fact, how you
confirmed it, and the exact re-check command if one applies — don't narrate the
process ("I then checked X, which told me Y") or restate context that's already
obvious. Dense and trustworthy beats padded, but don't cut so hard that a fact
loses the one command that would let you trust or re-verify it later.

**Keep each file's standing note short, not a repeat of this whole recipe.** The full
scan procedure lives here, in this file, permanently — it doesn't need to also live
inside the memory files themselves. At the top of each file you write, add one brief,
plain reminder that its contents can go stale and should be re-verified live before
being cited as current fact, rather than trusted blindly on a later session. Then, as
you write each section below that, attach the actual re-check command next to the
specific fact it validates (e.g. "installed-program versions drift — re-check via
`winget list` rather than trusting a version number recorded here") — that's more
useful later than one generic reminder at the top, because it tells your future self
exactly what to run, for exactly which claim, instead of "go re-scan everything"
again.

**This file itself doesn't expire after one use.** It normally only runs once,
automatically, the first time these two files are missing. But if a full fresh audit
is ever wanted later — after a major Windows upgrade, a hardware swap, or just because
the notes feel badly out of date rather than just one or two facts — it's fine to
re-run these exact instructions again on purpose, not just rely on the fact-by-fact
re-check hints already accumulated in the files.

**.mcp.json file in parent repor for this project** Please update the command and args 
path to match the users installation, otherwise this may cause issues when Claude tries
to add this MCP server. If you can not do such, then prompt the user to do it themselves.