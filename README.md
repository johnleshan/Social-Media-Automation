# Group Post Automator

A free, local, run-until-you-stop automation tool for posting your own images
and videos to Facebook groups you're a member of. Runs from your own machine
using your real logged-in Facebook session — no cloud, no API keys, no code
editing to switch accounts.

## Interface

The app opens a **modern web dashboard** in your browser (`python main.py`).
Everything is controlled from there — accounts, discovery, posting, live log
and group review. Legacy modes stay available: `python main.py --gui` (desktop
Tkinter) and `python main.py --cli` (console).

## What it does

- **Account plug-in** — each account is a Chrome profile. Log in once through
  the GUI, then switch between accounts from a dropdown. No cookies, no tokens
  to copy.
- **Group discovery** — search Facebook for groups by keyword (e.g. "investments
  kenya"). Two modes: plain search string, or a hard filter (keyword must be in
  the group name + minimum member count).
- **Approval pre-check** — reads each group's page before posting to decide if
  posts go through admin approval. Groups requiring approval are skipped and
  remembered forever; ambiguous groups are never auto-posted.
- **Continuous posting loop** — posts media from a folder, one file per group,
  with random delays, rotating content so a group never sees the same thing
  twice in a row. Runs until you press Stop; state is saved so you can resume.
- **Hidden mode** — runs headless by default so you can keep using your PC.

## Setup

1. **Install Python 3.10+** (3.12 tested) and run:

   ```
   pip install -r requirements.txt
   python -m playwright install chromium
   ```

   Real Chrome is used automatically when installed (recommended — Facebook's
   login and 2FA pages don't render properly in the bundled browser).

2. **Put your media** in the `content/` folder (jpg, png, gif, webp, mp4, mov...).

3. **Launch the tool:**

   ```
   python main.py
   ```

4. **Add an account — easiest (recommended):** click **Import from Chrome**.
   This copies your existing logged-in Facebook session from your normal
   Chrome into the bot profile — no login screen involved. **Close Chrome
   first**, then pick the account name and profile. The tool verifies the
   session automatically.

   *Alternative:* click **Add Account**, name it (e.g. `main`). A browser
   window opens — log into Facebook once, then close the window. If Facebook
   stalls after you enter your credentials, use **Import from Chrome** instead;
   the in-app login can be blocked by Facebook's bot detection.

## Daily use

1. Select the account profile.
2. Enter a search/keyword (or leave blank to reuse your saved group list).
3. Pick the media folder and optional caption.
4. Click **Scan Groups** to discover + classify groups, or click **Start
   Posting** (it will scan first if you entered a keyword).
5. Watch the log. Press **Stop** when done. Resume anytime — no duplicates.

Use the **Groups** tabs to review classified groups and manually override
status. **Settings** lets you tune delays, daily soft cap, and headless mode.

## Safety

- Posts are spaced by random delays (default 3–8 minutes) to stay under
  Facebook's radar.
- A daily soft cap (default 150) acts as a safety brake.
- If Facebook throws a checkpoint, the tool pauses and waits for you to resolve
  it instead of hammering through.

> Posting to groups you don't own is against Facebook's terms. Use at your own
> risk and only with accounts you're willing to lose.