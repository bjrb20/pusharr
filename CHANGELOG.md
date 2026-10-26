# Changelog

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
