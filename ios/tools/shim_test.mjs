// Checks the iPhone app's seams with the page and with iOS — runnable anywhere
// with Node, no Mac needed.
//
//     node ios/tools/shim_test.mjs
//
// 1. The JavaScript WebViewController injects into the warehouse page. It
//    replaces the page's scanner and reaches into the page's own globals
//    (openScanner, resolveCode, S, render, toast); rename one in
//    backend/app/mobile/index.html and the scan button quietly stops working.
//    The real injected code is run against stand-ins.
// 2. Info.plist: the permission texts iOS refuses to run without, and the ATS
//    trap — NSAllowsArbitraryLoads is silently IGNORED when
//    NSAllowsLocalNetworking or NSAllowsArbitraryLoadsInWebContent sits beside
//    it, which would break every LAN address.
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const IOS = join(HERE, '..');
const PAGE = join(IOS, '..', 'backend', 'app', 'mobile', 'index.html');
const SWIFT = readFileSync(join(IOS, 'Warexx', 'WebViewController.swift'), 'utf8');
const PLIST = readFileSync(join(IOS, 'Warexx', 'Info.plist'), 'utf8');

let fail = 0;
const ok = (name, cond, extra = '') => {
  console.log(`${cond ? 'PASS' : 'FAIL'}  ${name}${cond ? '' : '  ' + extra}`);
  if (!cond) fail++;
};

// --- the shim, as Swift holds it: a """ multi-line literal, indented by the
// closing delimiter's indentation, which Swift strips from every line ---------
const m = SWIFT.match(/static let scannerShim = """\n([\s\S]*?)\n([ \t]*)"""/);
if (!m) { console.error('FAIL  could not find scannerShim in WebViewController.swift'); process.exit(1); }
const indent = m[2];
const shim = m[1].split('\n').map((l) => (l.startsWith(indent) ? l.slice(indent.length) : l)).join('\n');
ok('shim has no backslashes (none would survive Swift escaping unchanged)', !shim.includes('\\'));

// --- the page still has to provide what the shim reaches for -----------------
const page = readFileSync(PAGE, 'utf8');
for (const [what, needle] of [
  ['openScanner', 'function openScanner('],
  ['resolveCode', 'async function resolveCode('],
  ['toast', 'function toast('],
  ['render', 'function render('],
  ['S (page state)', 'const S = {'],
  ["the page's own BarcodeDetector guard", "'BarcodeDetector' in window"],
]) {
  ok(`page still defines ${what}`, page.includes(needle),
     `"${needle}" is gone from backend/app/mobile/index.html — the iPhone scan button will stop working`);
}

// --- run it against stand-ins --------------------------------------------------
const PRODUCTS = { 'ESSA-00002': { sku: 'ESSA-00002' } };
let posts = [], toasts = [], renders = 0;
globalThis.window = globalThis;
globalThis.webkit = { messageHandlers: { essaScan: { postMessage: (m) => posts.push(m) } } };
globalThis.S = { selected: null, detailBack: null, screen: 'list' };
globalThis.render = () => { renders++; };
globalThis.toast = (m) => { toasts.push(m); };
globalThis.resolveCode = async (code) => PRODUCTS[code] || null;
globalThis.openScanner = () => { throw new Error('the page scanner ran — the shim did not replace it'); };

(0, eval)(shim);
ok('a BarcodeDetector exists, so the page takes its scanner path', 'BarcodeDetector' in globalThis);
(0, eval)(shim);
ok('injecting twice is harmless', typeof globalThis.__essaScanResult === 'function');

// no callback: scan → open that product
openScanner();
ok('openScanner posts to the essaScan handler', posts.length === 1 && posts[0] === 'scan');
await __essaScanResult('ESSA-00002');
ok('a known code opens its product', S.screen === 'detail' && S.selected?.sku === 'ESSA-00002' && renders === 1);
S.screen = 'list';
openScanner();
await __essaScanResult('NOPE');
ok('an unknown code says so', toasts.at(-1) === 'Scanned, but no product matches that code' && S.screen === 'list');

// with a callback: the raw payload goes back to the caller
let got = null;
openScanner((c) => { got = c; });
await __essaScanResult('RAW "quoted" \\ code');
ok('a callback receives the raw payload', got === 'RAW "quoted" \\ code');

// cancel (delivered as null) clears the pending callback
let stale = 0;
openScanner(() => { stale++; });
await __essaScanResult(null);
openScanner();
await __essaScanResult('ESSA-00002');
ok('a cancelled scan does not leave its callback to fire later', stale === 0);

// --- Info.plist -------------------------------------------------------------
const has = (k) => PLIST.includes(`<key>${k}</key>`);
for (const k of ['NSCameraUsageDescription', 'NSPhotoLibraryUsageDescription', 'NSLocalNetworkUsageDescription',
                 'UIApplicationSceneManifest', 'UILaunchScreen', 'UISupportedInterfaceOrientations~ipad']) {
  ok(`Info.plist has ${k}`, has(k));
}
ok('ATS: NSAllowsArbitraryLoads is not neutralised by a sibling key',
   !(has('NSAllowsArbitraryLoads') && (has('NSAllowsLocalNetworking') || has('NSAllowsArbitraryLoadsInWebContent'))),
   'with either sibling present iOS ignores NSAllowsArbitraryLoads and http://192.168.x.x stops loading');
ok('the scene delegate named in Info.plist exists', /class SceneDelegate\b/.test(
  readFileSync(join(IOS, 'Warexx', 'SceneDelegate.swift'), 'utf8')));

console.log(fail ? `\n${fail} FAILED` : '\nall passed');
process.exit(fail ? 1 : 0);
