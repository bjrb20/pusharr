// Unit test for static/detect.js with real-world user agents.
//   docker run --rm -v "$PWD":/w -w /w node:22-alpine node tests/test_detect.js
const { detectEnv } = require('../static/detect.js');
const UA = {
  iphoneSafari: 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.6 Mobile/15E148 Safari/604.1',
  iphoneSafari26: 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.0 Mobile/15E148 Safari/604.1',
  ipadOS: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.6 Safari/605.1.15',
  iphoneChrome: 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/141.0.7390.41 Mobile/15E148 Safari/604.1',
  iphoneFirefox: 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) FxiOS/143.0 Mobile/15E148 Safari/605.1.15',
  iphoneInstagram: 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 Instagram 400.0.0.0.0 (iPhone16,2; iOS 18_6; en_GB; en-GB; scale=3.00; 1290x2796; 0)',
  iphoneFacebook: 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 [FBAN/FBIOS;FBAV/500.0.0.0;FBBV/1;FBDV/iPhone16,2;FBMD/iPhone;FBSN/iOS;FBSV/18.6;FBSS/3;FBCR/;FBID/phone;FBLC/en_GB;FBOP/5]',
  iphoneWebView: 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148',
  iphoneGoogleApp: 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) GSA/390.0.0 Mobile/15E148 Safari/604.1',
  androidChrome: 'Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Mobile Safari/537.36',
  androidSamsung: 'Mozilla/5.0 (Linux; Android 14; SM-S921B) AppleWebKit/537.36 (KHTML, like Gecko) SamsungBrowser/28.0 Chrome/130.0.0.0 Mobile Safari/537.36',
  androidFirefox: 'Mozilla/5.0 (Android 14; Mobile; rv:143.0) Gecko/143.0 Firefox/143.0',
  androidWhatsApp: 'Mozilla/5.0 (Linux; Android 14; Pixel 8 Build/AP2A; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/141.0.0.0 Mobile Safari/537.36',
  androidInstagram: 'Mozilla/5.0 (Linux; Android 14; Pixel 8 Build/AP2A; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/141.0.0.0 Mobile Safari/537.36 Instagram 400.0.0.0.0 Android',
  desktopChrome: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36',
  desktopEdge: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36 Edg/141.0.0.0',
  desktopFirefox: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:143.0) Gecko/20100101 Firefox/143.0',
  macSafari: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.6 Safari/605.1.15',
};
const cases = [
  ['iphoneSafari', {}, 'ios-safari'], ['iphoneSafari26', {}, 'ios-safari'],
  ['ipadOS', { maxTouchPoints: 5 }, 'ios-safari'], ['iphoneChrome', {}, 'ios-other'], ['iphoneFirefox', {}, 'ios-other'],
  ['iphoneInstagram', {}, 'ios-other'], ['iphoneFacebook', {}, 'ios-other'], ['iphoneWebView', {}, 'ios-other'],
  ['iphoneGoogleApp', {}, 'ios-other'], ['androidChrome', {}, 'prompt'], ['androidSamsung', {}, 'prompt'],
  ['androidFirefox', {}, 'firefox-android'], ['androidWhatsApp', {}, 'android-inapp'], ['androidInstagram', {}, 'android-inapp'],
  ['desktopChrome', {}, 'prompt'], ['desktopEdge', {}, 'prompt'], ['desktopFirefox', {}, 'none'], ['macSafari', {}, 'mac-safari'],
  ['iphoneSafari', { standalone: true }, 'installed'], ['androidChrome', { standalone: true }, 'installed'],
];
let failed = 0;
for (const [name, opts, want] of cases) {
  const got = detectEnv(UA[name], opts).how;
  console.log(got === want ? 'ok  ' : 'FAIL', name.padEnd(16), opts.standalone ? '(installed)' : '', '->', got);
  if (got !== want) failed++;
}
UA.androidTV = 'Mozilla/5.0 (Linux; Android 12; BRAVIA 4K VH2 Build/STT1.211025.001.Z4) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36';
UA.fireTV = 'Mozilla/5.0 (Linux; Android 9; AFTKA Build/PS7633) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36';
const osCases = [['iphoneSafari', {}, 'ios'], ['ipadOS', { maxTouchPoints: 5 }, 'ios'], ['androidChrome', {}, 'android'],
  ['androidTV', {}, 'android-tv'], ['fireTV', {}, 'android-tv'], ['desktopChrome', {}, 'windows'], ['macSafari', {}, 'mac'],
  ['desktopFirefox', {}, 'windows']];
for (const [name, opts, want] of osCases) {
  const got = detectEnv(UA[name], opts).os;
  console.log(got === want ? 'ok  ' : 'FAIL', 'os', name.padEnd(14), '->', got);
  if (got !== want) failed++;
}
const ipad = detectEnv(UA.ipadOS, { maxTouchPoints: 5 });
if (!(ipad.ipad && ipad.platform === 'ios')) { console.log('FAIL iPad flags'); failed++; } else console.log('ok   iPad flags');
if (detectEnv(UA.macSafari, { maxTouchPoints: 0 }).platform !== 'desktop') { console.log('FAIL Mac is desktop'); failed++; } else console.log('ok   Mac is desktop');
console.log(failed ? `\n${failed} failed` : `\nall ${cases.length + osCases.length + 2} detection checks passed`);
process.exit(failed ? 1 : 0);
