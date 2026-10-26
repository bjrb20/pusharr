'use strict';
// Where are we running? Decides which install help to show. Pure function so it can be unit-tested
// (tests/test_detect.js) with real-world user agents.
//
// Returns { installed, platform: 'ios'|'android'|'desktop', os, browser, how } where `os` is
// ios/android/android-tv/windows/mac/linux/other and `how` is one of:
//   installed       running as the installed app
//   ios-safari      iPhone/iPad Safari: Share -> Add to Home Screen
//   ios-other       iOS Chrome/Firefox/Edge or an in-app browser: open in Safari first
//   android-inapp   Android in-app browser (WebView): open in Chrome first
//   prompt          Chromium (Android/desktop): beforeinstallprompt / browser menu
//   firefox-android Firefox on Android: menu -> Install
//   mac-safari      Safari on a Mac: File -> Add to Dock
//   none            can't install here (e.g. desktop Firefox); notifications still work in the tab

function detectEnv(ua, opts) {
  opts = opts || {};
  const standalone = !!opts.standalone;
  const touch = opts.maxTouchPoints || 0;
  const ipadOS = /Macintosh/.test(ua) && touch > 1;           // iPadOS asks for the desktop site
  const ios = /iPhone|iPad|iPod/.test(ua) || ipadOS;
  const android = /Android/.test(ua);
  const platform = ios ? 'ios' : android ? 'android' : 'desktop';
  const ipad = /iPad/.test(ua) || ipadOS;
  // finer OS, for picking the right app-store link
  const os = ios ? 'ios'
    : /Android TV|GoogleTV|Google TV|AFT[A-Z]|BRAVIA|SMART-TV|AndroidTV|; ?TV\)/.test(ua) ? 'android-tv'
      : android ? 'android'
        : /Windows NT/.test(ua) ? 'windows'
          : /Macintosh|Mac OS X/.test(ua) ? 'mac'
            : /Linux|X11|CrOS/.test(ua) ? 'linux' : 'other';
  const inApp = /FBAN|FBAV|FB_IAB|Instagram|WhatsApp|Line\/|Twitter|LinkedInApp|Snapchat|MicroMessenger|GSA\/|Pinterest|TikTok|musical_ly|DuckDuckGo\/\d/.test(ua);
  let browser = 'other';
  if (ios) {
    if (/CriOS/.test(ua)) browser = 'chrome';
    else if (/FxiOS/.test(ua)) browser = 'firefox';
    else if (/EdgiOS/.test(ua)) browser = 'edge';
    else if (/OPiOS|OPT\//.test(ua)) browser = 'opera';
    else if (inApp) browser = 'in-app';
    // Real Safari has both "Version/" and "Safari/"; app web views (WKWebView) usually lack them.
    else if (/Version\/[\d.]+.*Safari\//.test(ua)) browser = 'safari';
    else browser = 'in-app';
  } else if (inApp || (android && /; wv\)/.test(ua))) browser = 'in-app';
  else if (/Firefox\//.test(ua)) browser = 'firefox';
  else if (/SamsungBrowser/.test(ua)) browser = 'samsung';
  else if (/Edg\//.test(ua)) browser = 'edge';
  else if (/OPR\//.test(ua)) browser = 'opera';
  else if (/Chrome\/|Chromium\//.test(ua)) browser = 'chrome';
  else if (/Safari\//.test(ua)) browser = 'safari';

  let how;
  if (standalone) how = 'installed';
  else if (ios) how = browser === 'safari' ? 'ios-safari' : 'ios-other';
  else if (browser === 'in-app') how = android ? 'android-inapp' : 'none';
  else if (['chrome', 'edge', 'samsung', 'opera'].includes(browser)) how = 'prompt';
  else if (browser === 'firefox') how = android ? 'firefox-android' : 'none';
  else if (browser === 'safari' && platform === 'desktop') how = 'mac-safari';
  else how = 'none';
  return { installed: standalone, platform, browser, how, ipad, os };
}

if (typeof module !== 'undefined') module.exports = { detectEnv };
