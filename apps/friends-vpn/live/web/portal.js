import qrcode from '/qrcode.mjs';

const element = id => document.getElementById(id);
const form = element('enrollment');
const field = element('device');
const activate = element('activate');
const download = element('download');
let email;
let draft;
let replaces = '';
let busy = false;
let fileURL;

function notice(message, error = false) {
  const status = element('notice');
  status.hidden = !message;
  status.textContent = message;
  status.classList.toggle('error', error);
}

async function request(path, method = 'GET', body) {
  const response = await fetch('/api/' + path, {
    method,
    headers: body ? {'Content-Type': 'application/json'} : {},
    body: body ? JSON.stringify(body) : undefined,
    signal: AbortSignal.timeout(15000),
  });
  if (!response.ok) {
    const problem = await response.json().catch(() => ({}));
    throw new Error(problem.error || 'The request failed. Sign in again or retry in a moment.');
  }
  return response.status === 200 ? response.json() : response.status;
}

async function confirmed(operation) {
  for (let attempt = 0; attempt < 8; attempt++) {
    const result = await operation();
    if (result !== 202) return result;
    await new Promise(resolve => setTimeout(resolve, 1000));
  }
  throw new Error('The gateway is still applying this change. Retry to check it again.');
}

function stash(value) {
  draft = value;
  const key = `friends-vpn:setup:${email}`;
  try {
    if (value) sessionStorage.setItem(key, JSON.stringify(value));
    else sessionStorage.removeItem(key);
  } catch {
    // The open tab can finish setup when browser storage is disabled.
  }
}

function loadDraft() {
  try {
    return JSON.parse(sessionStorage.getItem(`friends-vpn:setup:${email}`));
  } catch {
    return null;
  }
}

function updateForm() {
  field.disabled = busy || !!draft;
  activate.disabled = busy;
  activate.textContent = draft ? 'Retry activation' : replaces ? 'Replace and activate' : 'Create and activate';
  if (!form.hidden) element('setup-heading').textContent = replaces ? 'Replace device profile' : 'Add a device';
  element('cancel').hidden = (!replaces && !draft) || busy;
  element('cancel').textContent = draft ? 'Cancel setup' : 'Cancel';
}

function button(label, style, action, deviceName) {
  const control = document.createElement('button');
  control.type = 'button';
  control.textContent = label;
  control.className = style;
  control.setAttribute('aria-label', `${label} ${deviceName}`);
  control.addEventListener('click', action);
  return control;
}

async function refresh() {
  const result = await request('devices');
  email = result.email;
  element('account').textContent = email;
  const list = element('devices');
  list.replaceChildren();
  element('empty').hidden = result.devices.length > 0;
  for (const device of result.devices) {
    const item = document.createElement('li');
    const description = document.createElement('span');
    const name = document.createElement('strong');
    const status = document.createElement('small');
    description.className = 'device';
    name.textContent = device.name;
    status.textContent = device.active ? 'Active' : 'Waiting for gateway';
    description.append(name, status);
    const actions = document.createElement('span');
    actions.className = 'actions';
    const replace = button('Replace', 'quiet', () => prepareReplacement(device), device.name);
    const revoke = button('Revoke', 'danger', () => revokeDevice(device), device.name);
    actions.append(replace, revoke);
    item.append(description, actions);
    list.append(item);
  }
}

function prepareReplacement(device) {
  if (busy || draft) {
    notice('Finish the current device setup first.');
    return;
  }
  replaces = device.id;
  field.value = device.name;
  updateForm();
  notice('Activating the replacement will revoke the old profile.');
  field.focus();
}

async function revokeDevice(device) {
  if (busy || !confirm(`Revoke the WireGuard profile for ${device.name}?`)) return;
  busy = true;
  updateForm();
  notice(`Revoking ${device.name}…`);
  try {
    await confirmed(() => request(`devices/${device.id}`, 'DELETE'));
    if (draft?.id === device.id) finish();
    await refresh();
    notice(`${device.name} was revoked.`);
  } catch (error) {
    notice(error.message, true);
  } finally {
    busy = false;
    updateForm();
  }
}

async function generateKeys() {
  try {
    const pair = await crypto.subtle.generateKey('X25519', true, ['deriveBits']);
    const publicBytes = new Uint8Array(await crypto.subtle.exportKey('raw', pair.publicKey));
    const jwk = await crypto.subtle.exportKey('jwk', pair.privateKey);
    const bytes = Uint8Array.from(atob(jwk.d.replaceAll('-', '+').replaceAll('_', '/')), c => c.charCodeAt(0));
    // WireGuard stores the clamped X25519 scalar in the profile.
    bytes[0] &= 248;
    bytes[31] = (bytes[31] & 127) | 64;
    return {privateKey: btoa(String.fromCharCode(...bytes)), publicKey: btoa(String.fromCharCode(...publicBytes))};
  } catch {
    throw new Error('Update your browser to generate a WireGuard profile, then try again.');
  }
}

function showProfile(config) {
  const contents = [
    '[Interface]', `PrivateKey = ${draft.privateKey}`, `Address = ${config.addresses.join(', ')}`,
    'DNS = 1.1.1.1', 'MTU = 1280', '', '[Peer]', `PublicKey = ${config.server_public_key}`,
    `PresharedKey = ${config.preshared_key}`, `Endpoint = ${config.endpoint}`,
    'AllowedIPs = 0.0.0.0/0, ::/0', 'PersistentKeepalive = 25', '',
  ].join('\n');
  if (fileURL) URL.revokeObjectURL(fileURL);
  fileURL = URL.createObjectURL(new Blob([contents], {type: 'application/octet-stream'}));
  download.href = fileURL;
  const filename = draft.name.toLowerCase().replace(/[^a-z0-9_-]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 15);
  download.download = `${filename || 'friends-vpn'}.conf`;

  const code = qrcode(0, 'M');
  code.addData(contents, 'Byte');
  code.make();
  const canvas = element('qr');
  const size = code.getModuleCount();
  const scale = 4;
  canvas.width = canvas.height = (size + 8) * scale;
  const context = canvas.getContext('2d');
  context.fillStyle = '#fff';
  context.fillRect(0, 0, canvas.width, canvas.height);
  context.fillStyle = '#000';
  for (let row = 0; row < size; row++) {
    for (let column = 0; column < size; column++) {
      if (code.isDark(row, column)) context.fillRect((column + 4) * scale, (row + 4) * scale, scale, scale);
    }
  }

  form.hidden = true;
  element('profile').hidden = false;
  element('setup-heading').textContent = 'Ready to import';
  download.focus();
}

async function activateDevice() {
  if (busy) return;
  busy = true;
  updateForm();
  notice('Activating your profile…');
  try {
    if (!draft) {
      const name = field.value.trim();
      if (!/^[\p{L}\p{N} ._()\-]{1,40}$/u.test(name)) {
        throw new Error('Use up to 40 letters, numbers, spaces, hyphens, dots, underscores or parentheses.');
      }
      const keys = await generateKeys();
      stash({id: crypto.randomUUID(), name, replaces, ...keys});
    }
    const result = await confirmed(() => request(`devices/${draft.id}`, 'PUT', {
      name: draft.name,
      public_key: draft.publicKey,
      replaces: draft.replaces,
    }));
    notice('');
    showProfile(result);
    await refresh();
  } catch (error) {
    notice(error.message, true);
  } finally {
    busy = false;
    updateForm();
  }
}

function finish() {
  stash(null);
  replaces = '';
  form.reset();
  if (fileURL) URL.revokeObjectURL(fileURL);
  fileURL = undefined;
  download.removeAttribute('href');
  element('qr').width = 0;
  element('qr-section').open = false;
  element('profile').hidden = true;
  form.hidden = false;
  updateForm();
}

async function cancelSetup() {
  if (busy) return;
  if (!draft) {
    finish();
    notice('');
    return;
  }
  if (!confirm('Cancel setup and revoke any profile already created for this device?')) return;
  busy = true;
  updateForm();
  try {
    await confirmed(() => request(`devices/${draft.id}`, 'DELETE'));
    finish();
    await refresh();
    notice('Device setup canceled.');
  } catch (error) {
    if (error.message === 'Device not found.') {
      finish();
      notice('Device setup canceled.');
    } else {
      notice(error.message, true);
    }
  } finally {
    busy = false;
    updateForm();
  }
}

async function start() {
  element('reload').hidden = true;
  notice('Loading your devices…');
  try {
    await refresh();
    draft = loadDraft();
    if (draft) {
      field.value = draft.name;
      replaces = draft.replaces;
    }
    updateForm();
    element('panels').hidden = false;
    notice(draft ? 'Continue setting up your current profile.' : '');
  } catch (error) {
    notice(error.message, true);
    element('reload').hidden = false;
  }
}

form.addEventListener('submit', event => {
  event.preventDefault();
  activateDevice();
});
element('cancel').addEventListener('click', cancelSetup);
element('done').addEventListener('click', () => {
  finish();
  field.focus();
});
element('logout').addEventListener('click', finish);
element('reload').addEventListener('click', start);
start();
