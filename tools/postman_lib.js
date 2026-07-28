/* GL — shared helper library for the Augmont Gold Loan Postman collection.
 *
 * This file is the SOURCE OF TRUTH for the collection's JavaScript. It is embedded verbatim
 * into the generated collection (as the GL_LIB collection variable) by
 * tools/build_postman_collection.py, and every request script starts with:
 *
 *     eval(pm.collectionVariables.get("GL_LIB"));
 *
 * It is embedded as a JSON string literal (NOT a template literal — that would eat the
 * backslashes and silently destroy every regex below), so the only real constraint is:
 *   - No ES6+ syntax that older Newman sandboxes may reject (use var/function).
 */
var GL = (function () {
  var C = (typeof CryptoJS !== 'undefined') ? CryptoJS : require('crypto-js');

  /* ---------- variables ---------- */
  function get(k, d) {
    var v = pm.collectionVariables.get(k);
    if (v === undefined || v === null || v === '') { return (d === undefined ? '' : d); }
    return v;
  }
  function set(k, v) {
    pm.collectionVariables.set(k, (v === undefined || v === null) ? '' : String(v));
  }
  function setJson(k, v) { pm.collectionVariables.set(k, JSON.stringify(v)); }
  function getJson(k, d) {
    var raw = pm.collectionVariables.get(k);
    if (!raw) { return d; }
    try { return JSON.parse(raw); } catch (e) { return d; }
  }
  function num(v, d) { var n = parseFloat(v); return isNaN(n) ? (d === undefined ? 0 : d) : n; }
  function int(v, d) {
    if (v === '' || v === null || v === undefined) { return (d === undefined ? null : d); }
    var n = parseInt(v, 10);
    return isNaN(n) ? (d === undefined ? null : d) : n;
  }
  function round2(v) { return Math.round((num(v) + Number.EPSILON) * 100) / 100; }
  function money(v) { return num(v).toFixed(2); }
  function log() {
    var parts = [];
    for (var i = 0; i < arguments.length; i++) { parts.push(String(arguments[i])); }
    console.log('[GL] ' + parts.join(' '));
  }

  /* ---------- URLs ---------- */
  function baseUrl() { return String(get('base_url')).replace(/\/+$/, ''); }
  function fullUrl(p) { return p ? (baseUrl() + '/' + p) : null; }

  /* The backend signs the RELATIVE path + query string, with %20 rendered as a literal space
   * (matching the browser). Resolve variables first so the signed path is the sent path. */
  function pathForSign() {
    var full = pm.variables.replaceIn(pm.request.url.toString());
    var base = baseUrl();
    if (base && full.indexOf(base) === 0) { full = full.substring(base.length); }
    if (full.indexOf('http') === 0) {
      var i = full.indexOf('/', 8);
      full = (i >= 0) ? full.substring(i) : '/';
    }
    if (full.charAt(0) !== '/') { full = '/' + full; }
    return full.split('%20').join(' ');
  }

  /* ---------- HMAC signing ----------
   * signature = HMAC-SHA256( JSON.stringify({...body, url: path}), secret ).hex
   * GET requests and multipart uploads sign an EMPTY body, exactly like the harness. */
  function signPayload(bodyObj) {
    var payload = {};
    if (bodyObj) {
      for (var k in bodyObj) {
        if (Object.prototype.hasOwnProperty.call(bodyObj, k)) { payload[k] = bodyObj[k]; }
      }
    }
    payload.url = pathForSign();
    var sig = C.HmacSHA256(JSON.stringify(payload), get('hmac_secret')).toString(C.enc.Hex);
    pm.request.headers.upsert({ key: 'signature', value: sig });
    return sig;
  }
  function sign() { return signPayload(null); }

  /* Build the JSON body AND sign it from the same object, so the bytes sent are the bytes
   * signed. Every JSON request in the collection has raw body {{__body}}. JSON.stringify also
   * gives us JS number rendering (4480, not 4480.0) for free — the harness has to emulate this
   * with _js_number_normalize. */
  function body(obj) {
    pm.collectionVariables.set('__body', JSON.stringify(obj));
    signPayload(obj);
    return obj;
  }

  /* ---------- roles ----------
   * Every request sends Authorization: Bearer {{token}}; role() points {{token}} at the token
   * captured for that role in the Logins folder. */
  function role(name) {
    var t = get('token_' + name);
    if (!t) { throw new Error('No token stored for role "' + name + '". Run the 00-Logins folder.'); }
    set('token', t);
    return t;
  }
  function jwtPayload(token) {
    try {
      var p = String(token).split('.')[1].replace(/-/g, '+').replace(/_/g, '/');
      return JSON.parse(Buffer.from(p, 'base64').toString('utf8'));
    } catch (e) { return {}; }
  }

  /* ---------- CryptoJS AES (identity-proof numbers AND file paths) ----------
   * Mirrors the Node/crypto-js call the harness shells out to; produces OpenSSL
   * "U2FsdGVkX1..." ciphertext the backend can decrypt. */
  function enc(value) {
    if (value === null || value === undefined || value === '') { return value; }
    var key = C.HmacSHA256('TSHZ2_AUGMONT_CY2RG', '').toString().substring(0, 32);
    var iv = C.HmacSHA256('4WI3R_AUGMONT_OVEEC', '');
    return C.AES.encrypt(String(value), key, {
      keySize: 16, iv: iv, mode: C.mode.ECB, padding: C.pad.Pkcs7, algorithm: 'AES-256-CBC'
    }).toString();
  }

  /* ---------- random test data ---------- */
  var FIRST = ['John', 'Jane', 'Peter', 'Mary', 'Robert', 'Patricia', 'Michael', 'Linda'];
  var LAST = ['Doe', 'Smith', 'Jones', 'Williams', 'Brown', 'Davis', 'Miller', 'Wilson'];
  var STREETS = ['MG Road', 'Park Street', 'Church Street', 'Link Road', 'Main Street'];
  var LANDMARKS = ['GT Circle', 'City Mall', 'Bus Depot', 'Central Park', 'Old Temple'];
  var CITIES = ['Mumbai', 'Delhi', 'Bangalore', 'Chennai', 'Hyderabad'];
  var STATES = ['Maharashtra', 'Karnataka', 'Tamil Nadu', 'Telangana', 'West Bengal'];
  var LETTERS = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ';

  function ri(a, b) { return Math.floor(Math.random() * (b - a + 1)) + a; }
  function pick(arr) { return arr[Math.floor(Math.random() * arr.length)]; }
  function uniform(a, b) { return a + Math.random() * (b - a); }
  function firstName() { return pick(FIRST); }
  function lastName() { return pick(LAST); }
  function fullName() { return pick(FIRST) + ' ' + pick(LAST); }
  function mobile() { return String(ri(7, 9)) + String(ri(100000000, 999999999)); }
  function pincode() { return String(ri(100000, 999999)); }
  function pan() {
    var s = '';
    for (var i = 0; i < 3; i++) { s += pick(LETTERS.split('')); }
    s += 'P';
    s += pick(LETTERS.split(''));
    for (var j = 0; j < 4; j++) { s += String(ri(0, 9)); }
    s += pick(LETTERS.split(''));
    return s;
  }
  function address() {
    var landmark = pick(LANDMARKS);
    return {
      address: String(ri(1, 200)) + ', ' + pick(STREETS) + ', near ' + landmark + ', ' +
               pick(CITIES) + ', ' + pick(STATES) + ' - ' + String(ri(100000, 999999)),
      landmark: landmark
    };
  }
  function dobAge() {
    var age = ri(24, 58);
    var d = new Date();
    d.setDate(d.getDate() - (age * 365 + ri(0, 364)));
    return { age: String(age), dob: d.toISOString().substring(0, 10) };
  }
  /* Aadhaar carries a Verhoeff check digit; the KYC API validates the format. */
  function aadhaar() {
    var m = [[0,1,2,3,4,5,6,7,8,9],[1,2,3,4,0,6,7,8,9,5],[2,3,4,0,1,7,8,9,5,6],
             [3,4,0,1,2,8,9,5,6,7],[4,0,1,2,3,9,5,6,7,8],[5,9,8,7,6,0,4,3,2,1],
             [6,5,9,8,7,1,0,4,3,2],[7,0,4,6,1,3,5,8,2,9],[8,7,6,5,9,3,2,1,0,4],
             [9,8,7,6,5,4,3,2,1,0]];
    var p = [[0,1,2,3,4,5,6,7,8,9],[1,5,7,6,2,8,3,0,9,4],[5,8,0,3,7,9,6,1,4,2],
             [8,9,1,6,0,4,3,5,2,7],[9,4,5,3,1,2,6,8,7,0],[4,2,8,6,5,7,3,9,0,1],
             [2,7,9,3,8,0,6,4,1,5],[7,0,4,6,1,3,5,8,2,9]];
    var inv = [0,4,3,2,1,5,6,7,8,9];
    var digits = '';
    for (var i = 0; i < 11; i++) { digits += String(i === 0 ? ri(2, 9) : ri(0, 9)); }
    var c = 0;
    var rev = digits.split('').reverse();
    for (var k = 0; k < rev.length; k++) {
      c = m[c][p[(k + 1) % 8][parseInt(rev[k], 10)]];
    }
    return digits + String(inv[c]);
  }
  function utrNumber() {
    var s = '';
    for (var i = 0; i < 15; i++) { s += String(i === 0 ? ri(1, 9) : ri(0, 9)); }
    return s;
  }
  function digits(n) {
    var s = '';
    for (var i = 0; i < n; i++) { s += String(ri(0, 9)); }
    return s;
  }
  function todayIso() { return new Date().toISOString().substring(0, 10); }
  function nowIsoZ() { return new Date().toISOString().replace(/\.\d+Z$/, '.000Z'); }
  function baseName(p) { return p ? String(p).split('/').pop().split('\\').pop() : p; }

  /* ---------- response helpers ---------- */
  function json() {
    try { return pm.response.json(); } catch (e) { return {}; }
  }
  function findKey(obj, key) {
    if (obj && typeof obj === 'object' && !Array.isArray(obj)) {
      if (Object.prototype.hasOwnProperty.call(obj, key) &&
          (obj[key] === null || typeof obj[key] !== 'object')) { return obj[key]; }
      for (var k in obj) {
        if (Object.prototype.hasOwnProperty.call(obj, k)) {
          var f = findKey(obj[k], key);
          if (f !== null && f !== undefined) { return f; }
        }
      }
    } else if (Array.isArray(obj)) {
      for (var i = 0; i < obj.length; i++) {
        var g = findKey(obj[i], key);
        if (g !== null && g !== undefined) { return g; }
      }
    }
    return null;
  }
  /* Locate the server's stored-ornament list (real DB ids + calculated amounts). */
  function findOrnaments(obj) {
    var markers = ['netWtAfterPurity', 'currentLtvAmount', 'ornamentTypeId', 'grossWeight'];
    if (Array.isArray(obj)) {
      if (obj.length && obj[0] && typeof obj[0] === 'object') {
        for (var m = 0; m < markers.length; m++) {
          if (Object.prototype.hasOwnProperty.call(obj[0], markers[m])) { return obj; }
        }
      }
      for (var i = 0; i < obj.length; i++) {
        var f = findOrnaments(obj[i]);
        if (f) { return f; }
      }
    } else if (obj && typeof obj === 'object') {
      for (var k in obj) {
        if (Object.prototype.hasOwnProperty.call(obj, k)) {
          var g = findOrnaments(obj[k]);
          if (g) { return g; }
        }
      }
    }
    return null;
  }
  /* '22 K HM' -> 22. Must read the LEADING integer; a naive replace('K','') yields 0. */
  function karatValue(s) {
    if (!s) { return 0; }
    var m = String(s).match(/\d+/);
    return m ? parseInt(m[0], 10) : 0;
  }

  /* ---------- assertions ---------- */
  function ok(label) {
    pm.test(label + ' [' + pm.response.code + ']', function () {
      pm.expect(pm.response.code, pm.response.text().substring(0, 400)).to.be.oneOf([200, 201]);
    });
    return pm.response.code === 200 || pm.response.code === 201;
  }
  /* Steps the harness treats as non-fatal (it catches the HTTPStatusError and continues). */
  function soft(label) {
    pm.test(label + ' (non-fatal) [' + pm.response.code + ']', function () { pm.expect(true).to.be.true; });
    if (pm.response.code >= 400) { log('non-fatal failure:', label, pm.response.text().substring(0, 200)); }
    return pm.response.code === 200 || pm.response.code === 201;
  }

  return {
    get: get, set: set, setJson: setJson, getJson: getJson,
    num: num, int: int, round2: round2, money: money, log: log,
    baseUrl: baseUrl, fullUrl: fullUrl, sign: sign, body: body, signPayload: signPayload,
    role: role, jwtPayload: jwtPayload, enc: enc,
    ri: ri, pick: pick, uniform: uniform, firstName: firstName, lastName: lastName,
    fullName: fullName, mobile: mobile, pincode: pincode, pan: pan, address: address,
    dobAge: dobAge, aadhaar: aadhaar, utrNumber: utrNumber, digits: digits,
    todayIso: todayIso, nowIsoZ: nowIsoZ, baseName: baseName,
    json: json, findKey: findKey, findOrnaments: findOrnaments, karatValue: karatValue,
    ok: ok, soft: soft
  };
}());
