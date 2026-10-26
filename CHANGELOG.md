# Changelog

## 1.1.0 (2026-10-27)

- **Exact app links:** recommended apps can have a `play.item` template with `{server_id}` and
  `{item_id}`, e.g. Plezy's `plezy://play?server={server_id}&id={item_id}`. The server id is Jellyfin's
  own `Id`, which is what Plezy registers a Jellyfin server under. "▶ Play in …" then opens the exact
  episode or film; for a batch, the first new episode. The TMDB/TVDB links stay as the fallback while
  the item isn't known.
- **Alerts wait until the content is really ready** (`notifications.wait_for_ready`).
  - **Which alerts:** new-episode, new-film/show and request-ready alerts are held until Jellyfin has
    the item with its title, overview and poster (for episodes, also their series).
  - **Checking:** every 2 minutes at first, backing off to every 30 minutes.
  - **Nudging Jellyfin:** after 10 minutes it's told about the new files, or asked to refresh the item.
  - **Giving up:** after 6 hours the alert goes anyway, and its page still finds the item later.
  - **Batches** of the same show keep merging while they wait.
  - **Restarts:** the waiting list is in the database, so it survives them.
  - **Unidentified items:** shows or films Jellyfin hasn't identified yet (no provider ids) are found
    by their folder, since Sonarr's and Radarr's paths are Jellyfin's.
  - **New command:** `notifier.py waiting`.

## 1.0.1 (2026-10-27)

- **Every push opens a note page with the item's buttons.** Before, alerts whose Jellyfin item wasn't
  found at send time (new shows and films still being scanned, request-ready alerts without a Jellyfin
  id) opened the home page, with no Watch, app or request buttons.
- **Late lookup:** the note page now looks the item up by its TMDB/TVDB id when it opens (the episode
  itself for a single new episode), retries for a while, and stores the link once found.
  `core.backfill_notes()` fills in stored notes.
- **Taps open the note even when the installed app is already open.** The service worker no longer
  relies on memory it loses when the browser stops it. It asks the open windows which one is the
  installed app, and the app asks for the tapped note when it comes to the front.
- New tests for each notification type's note page, and for notification taps (`tests/test_sw.js`).

## 1.0.0 (2026-10-27)

First release as **Pusharr**: the app is now white-label, and one config file describes an instance.

- **One config file** (`config.yaml`) for everything except secrets, merged over neutral defaults and
  validated (unknown keys, colours, https links, template variables, ports, time zone). The
  environment is for secrets only.
- **Branding & settings page** for admins: a form or raw YAML, with validation before anything is
  saved and the previous file kept as `config.yaml.bak`. Changes apply at once.
- **Branding:** names, about 20 theme colours, bundled or uploaded fonts, and a text or image
  wordmark. The icon is one uploaded SVG, which becomes every PWA size (any, maskable, Apple touch,
  favicon) plus the monochrome badge.
- **Links:** Watch and Request links and item templates. Recommended apps have per-platform store
  links and optional deep links ("▶ Play in <app>"), and alternative apps can be listed.
- **Messages:** the title and text of every notification is a template with variables.
- **Notification types** can be switched off or have new defaults. Digest hour, time zone, batching
  thresholds and follow rules are settings.
- **Integrations** (Seerr/Jellyseerr/Overseerr, Sonarr, Radarr, Bazarr, Wizarr sync) each have an
  on/off switch and a URL.
- **Approvers** are Jellyfin admins or a list of usernames. The replacement picker's languages and
  rejected tags are configurable.
- **Sign-in** methods (password, Quick Connect) can be switched on or off.
- **Web push is the only channel:** the Home Assistant delivery, its issue-approval path and its
  settings are gone.
- **Removed:** the one-off import of a stand-alone notifier's JSON state.
- **New:** `tools/demo.py` (try it with a fake Jellyfin), a Docker Compose example, `.env.example`,
  `config.example.yaml`, the MIT licence and this changelog.

Before 1.0.0 this was a single-instance app with its branding built in.
