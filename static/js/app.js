(() => {
  'use strict';

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const icon = (name, extra = '') => `<svg class="icon ${extra}" aria-hidden="true"><use href="#i-${name}"></use></svg>`;
  const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  const state = {
    status: null, files: [], devices: [], events: [], transfers: [],
    view: 'library', filter: 'all', query: '', display: 'grid', selected: new Set(),
    openMenu: null, activityTab: 'events', modalFiles: [], lastLibraryRender: '', lastDeviceRender: '',
    lastActivityRender: '', failed: false, refreshing: false,
  };
  const modal = $('#modal');
  const fileInput = $('#file-input');
  const cloudPreview = window.location.hostname.endsWith('.e2b.app');

  function prettySize(size) {
    if (!size) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.min(Math.floor(Math.log(size) / Math.log(1024)), units.length - 1);
    const value = size / Math.pow(1024, i);
    return `${i && value < 10 ? value.toFixed(1) : Math.round(value)} ${units[i]}`;
  }
  function timeAgo(date) {
    const seconds = Math.max(0, Math.floor((Date.now() - new Date(date).getTime()) / 1000));
    if (!Number.isFinite(seconds)) return '';
    if (seconds < 60) return 'Just now';
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
    if (seconds < 604800) return `${Math.floor(seconds / 86400)}d ago`;
    return new Date(date).toLocaleDateString(undefined, {month: 'short', day: 'numeric'});
  }
  function toast(message, error = false) {
    const node = document.createElement('div');
    node.className = `toast${error ? ' error' : ''}`;
    node.textContent = message;
    $('#toast-region').append(node);
    setTimeout(() => node.remove(), error ? 6500 : 4200);
  }
  async function api(url, options = {}) {
    const headers = new Headers(options.headers || {});
    const method = (options.method || 'GET').toUpperCase();
    if (!['GET', 'HEAD'].includes(method)) headers.set('X-Requested-With', 'XMLHttpRequest');
    if (options.json !== undefined) {
      headers.set('Content-Type', 'application/json');
      options.body = JSON.stringify(options.json);
      delete options.json;
    }
    const response = await fetch(url, {...options, headers, cache: 'no-store'});
    let data = {};
    try { data = await response.json(); } catch (_) { /* non-JSON server errors */ }
    if (!response.ok) {
      const error = new Error(data.error || `Request failed (${response.status}).`);
      error.data = data;
      throw error;
    }
    return data;
  }
  function wirelessAddress() {
    const wireless = state.status?.wireless;
    return wireless ? `${wireless.host}:${wireless.port}` : 'Checking address...';
  }
  function catalogAddress() { return state.status?.opds_url || 'Checking address...'; }
  function downloadUrl(file) { return `/get/${encodeURIComponent(file.id)}/${encodeURIComponent(file.filename)}`; }
  function statusDot(el, online) { el.classList.toggle('offline', !online); }

  async function refresh() {
    if (state.refreshing) return;
    state.refreshing = true;
    try {
      const [status, files, devices, activity] = await Promise.all([
        api('/api/status'), api('/api/files'), api('/api/devices'), api('/api/activity')
      ]);
      state.status = status;
      state.files = files.files || [];
      state.devices = devices.devices || [];
      state.events = activity.events || [];
      state.transfers = activity.transfers || [];
      const existing = new Set(state.files.map(f => f.id));
      for (const id of state.selected) if (!existing.has(id)) state.selected.delete(id);
      state.failed = false;
      renderStatus();
      renderLibrary();
      renderDevices();
      renderActivity();
    } catch (error) {
      if (!state.failed) toast('Could not reach the server. Trying again...', true);
      state.failed = true;
      $('#sidebar-status-title').textContent = 'Server unavailable';
      $('#top-status-text').textContent = 'Server unavailable';
      $('#stat-server').textContent = 'Offline';
      statusDot($('#sidebar-dot'), false);
      statusDot($('#top-dot'), false);
    } finally { state.refreshing = false; }
  }
  function renderStatus() {
    const s = state.status;
    if (!s) return;
    const online = !!s.wireless.running;
    $('#nav-file-count').textContent = s.file_count;
    $('#nav-device-count').textContent = state.devices.length;
    $('#stat-files').textContent = s.file_count;
    $('#stat-files-unit').textContent = s.file_count === 1 ? 'file' : 'files';
    $('#stat-devices').textContent = state.devices.length;
    $('#stat-server').textContent = online ? 'Online' : 'Offline';
    $('#stat-port').textContent = `Port ${s.wireless.port}`;
    $('#sidebar-status-title').textContent = online ? 'Server is on' : 'Needs attention';
    $('#sidebar-address').textContent = wirelessAddress();
    $('#top-status-text').textContent = online ? 'Wireless server is on' : 'Wireless server unavailable';
    statusDot($('#sidebar-dot'), online);
    statusDot($('#top-dot'), online);
    $('#rail-address').textContent = wirelessAddress();
    $('#rail-live').innerHTML = `<span class="status-dot ${online ? '' : 'offline'}"></span>${online ? 'SERVER ON' : 'SERVER OFF'}`;
    $('.connection-card > p').textContent = online ? 'Your wireless server is waiting for your reader to say hello.' : 'The wireless server could not start. Check the port in Settings.';
    $('#rail-activity').innerHTML = state.events.length ? state.events.slice(0, 3).map(event => `
      <div class="rail-event"><span class="rail-event-icon">${icon(eventIcon(event.kind))}</span><div class="rail-event-text"><strong>${esc(event.message)}</strong><small>${esc(event.detail || timeAgo(event.created_at))} · ${esc(timeAgo(event.created_at))}</small></div></div>`).join('') : '<p class="rail-empty">Your little updates will appear here.</p>';
  }
  function eventIcon(kind) {
    return ({upload:'upload', transfer:'arrow', device:'device', delete:'trash', error:'help', server:'wifi'})[kind] || 'clock';
  }
  function filterFiles() {
    const q = state.query.trim().toLocaleLowerCase();
    return state.files.filter(file => (state.filter === 'all' || file.category === state.filter) &&
      (!q || [file.title, file.author, file.filename, file.extension].some(text => String(text || '').toLocaleLowerCase().includes(q))));
  }
  function renderLibrary() {
    const key = JSON.stringify([state.files, state.filter, state.query, state.display, [...state.selected], state.openMenu]);
    if (key === state.lastLibraryRender) return;
    state.lastLibraryRender = key;
    $('#collection-count').textContent = state.files.length;
    $$('.filter-tabs button').forEach(btn => btn.classList.toggle('active', btn.dataset.filter === state.filter));
    $('[data-action="view-grid"]').classList.toggle('active', state.display === 'grid');
    $('[data-action="view-list"]').classList.toggle('active', state.display === 'list');
    $('#selection-bar').hidden = state.selected.size === 0;
    $('#selection-count').textContent = state.selected.size;
    const files = filterFiles();
    const root = $('#collection-content');
    if (!state.files.length && !state.query && state.filter === 'all') {
      root.innerHTML = `<div class="empty-library"><div class="paper-stack" aria-hidden="true"><span class="paper paper-one">PDF</span><span class="paper paper-two">IMG</span><span class="paper paper-three">EPUB</span></div><h3>Every good collection starts somewhere.</h3><p>Drop your files here, or browse to add your first one.</p><div class="empty-actions"><button class="button button-primary" data-action="upload">${icon('plus')} Browse files</button><button class="text-button" data-action="sample">Try a sample file →</button></div><div class="format-note">EPUB <span>•</span> PDF <span>•</span> CBZ <span>•</span> images <span>•</span> and anything else</div></div>`;
      return;
    }
    if (!files.length) {
      root.innerHTML = `<div class="empty-results"><h3>No files found just yet.</h3><p>Try another search or choose a different category.</p><button class="text-button" data-action="reset-filter">See all files →</button></div>`;
      return;
    }
    root.innerHTML = `<div class="${state.display === 'grid' ? 'file-grid' : 'file-list'}">${files.map(fileCard).join('')}</div>`;
  }
  function fileCard(file) {
    const selected = state.selected.has(file.id);
    const color = parseInt(file.id.replace(/-/g, '').slice(0, 3), 16) % 6;
    const format = file.extension ? file.extension.toUpperCase() : 'FILE';
    const author = file.author || file.filename;
    const image = file.has_cover ? `<img src="/api/files/${encodeURIComponent(file.id)}/cover" alt="" loading="lazy">` : '';
    const cover = `<div class="file-cover cover-color-${color} ${file.has_cover ? 'has-image' : ''}">${image}<span class="cover-top">LEAFLINE COLLECTION</span><div class="cover-art">${esc(file.title)}<span class="cover-author">${esc(file.author || 'A little something to read')}</span></div><span class="cover-bottom">✳ &nbsp; YOUR LIBRARY</span><span class="file-format">${esc(format)}</span></div>`;
    const menu = state.openMenu === file.id ? `<div class="file-menu" role="menu"><button data-action="send-file" data-id="${esc(file.id)}" role="menuitem">${icon('arrow')} Send to device</button><a href="${esc(downloadUrl(file))}" download role="menuitem">${icon('download')} Download file</a><button data-action="edit-file" data-id="${esc(file.id)}" role="menuitem">${icon('edit')} Edit details</button><button class="danger" data-action="delete-file" data-id="${esc(file.id)}" role="menuitem">${icon('trash')} Remove file</button></div>` : '';
    const checkbox = `<input class="file-check" type="checkbox" data-select="${esc(file.id)}" aria-label="Select ${esc(file.title)}" ${selected ? 'checked' : ''}>`;
    const meta = `<div class="file-meta"><div class="file-meta-text"><strong title="${esc(file.title)}">${esc(file.title)}</strong><small title="${esc(author)}">${esc(author)}</small></div>${state.display === 'grid' ? `<button class="icon-button file-more" data-action="menu" data-id="${esc(file.id)}" aria-label="Actions for ${esc(file.title)}" aria-expanded="${state.openMenu === file.id}">${icon('more')}</button>` : ''}</div>`;
    if (state.display === 'list') return `<article class="file-card">${checkbox}${cover}${meta}<span class="list-format">${esc(format)}</span><span class="list-size">${esc(prettySize(file.size))}</span><button class="icon-button file-more" data-action="menu" data-id="${esc(file.id)}" aria-label="Actions for ${esc(file.title)}" aria-expanded="${state.openMenu === file.id}">${icon('more')}</button>${menu}</article>`;
    return `<article class="file-card">${checkbox}${cover}${meta}${menu}</article>`;
  }
  function renderDevices() {
    const key = JSON.stringify([state.devices, state.status]);
    if (key === state.lastDeviceRender) return;
    state.lastDeviceRender = key;
    const online = !!state.status?.wireless?.running;
    const devices = state.devices.length ? state.devices.map(d => `<div class="device-item"><span class="panel-icon">${icon('device')}</span><div class="device-item-text"><strong>${esc(d.name)}</strong><small>${esc(d.address)}${d.busy ? ' · Sending a file' : d.closing ? ' · Disconnecting' : ' · Connected'}</small></div><button class="disconnect-btn" data-action="disconnect" data-id="${esc(d.id)}">Disconnect</button></div>`).join('') : `<div class="no-devices"><div class="no-device-illustration">${icon('device')}</div><h3>No readers connected yet.</h3><p>Follow the steps to bring your reader a little closer.</p></div>`;
    $('#devices-content').innerHTML = `<div class="devices-grid"><section class="content-panel connect-panel"><div class="panel-topline"><span class="panel-icon">${icon('wifi')}</span><span class="live-pill"><span class="status-dot ${online ? '' : 'offline'}"></span>${online ? 'SERVER ON' : 'SERVER OFF'}</span></div><div class="panel-kicker">WIRELESS CONNECTION</div><h2 class="panel-title">A little connection goes a long way.</h2><p class="panel-description">Use a Calibre wireless-device client to connect your reader, then send files directly from your library.</p><ol class="steps"><li class="step"><span class="step-num">1</span><div><strong>Join the same Wi-Fi network</strong><p>Connect this server and your e-reader to the same local network.</p></div></li><li class="step"><span class="step-num">2</span><div><strong>Connect from your reader</strong><p>In KOReader, open Calibre → Connect. In Calibre Companion, connect as a wireless device. It should discover the server automatically.</p></div></li><li class="step"><span class="step-num">3</span><div><strong>Not seeing the server?</strong><p>Set a custom Calibre address on your reader using the IP and wireless port below.</p></div></li></ol><div class="copy-row"><div><small>WIRELESS ADDRESS · IP : PORT</small><strong>${esc(wirelessAddress())}</strong></div><button class="icon-button" data-action="copy-wireless" aria-label="Copy wireless address">${icon('copy')}</button></div>${!online && state.status?.wireless?.error ? `<div class="security-box">Could not start the wireless server: ${esc(state.status.wireless.error)}. Try another port or stop the other Calibre server.</div>` : ''}${cloudPreview ? `<div class="security-box"><strong>Preview note:</strong> This server is running in a remote workspace, not on your Wi-Fi. Run Leafline on a computer on the reader’s local network to connect a physical device.</div>` : ''}</section><div><section class="content-panel device-panel"><div class="panel-kicker">YOUR READERS</div><div class="device-panel-heading"><h2>Connected devices</h2><span class="device-count">${state.devices.length} CONNECTED</span></div>${devices}</section><div class="compatibility-note">${icon('spark')}<span><strong>A little note.</strong> Wireless support depends on the app running on your reader. Your device decides which file formats it accepts. Existing files with the same name may be replaced.</span></div></div></div><section class="content-panel opds-panel"><span class="panel-icon">${icon('book')}</span><div class="opds-info"><h3>Prefer to browse instead?</h3><p>Add this OPDS catalog to a compatible reader, or open the web address in your device's browser to download files.</p></div><div class="opds-url">${esc(catalogAddress())}</div><button class="button button-outline" data-action="copy-opds">${icon('copy')} Copy URL</button></section>`;
  }
  function renderActivity() {
    const key = JSON.stringify([state.events, state.transfers, state.activityTab]);
    if (key === state.lastActivityRender) return;
    state.lastActivityRender = key;
    let rows;
    if (state.activityTab === 'transfers') {
      rows = state.transfers.length ? state.transfers.map(job => {
        const label = ({queued:'Queued', sending:'Sending', sent:'Sent', failed:'Failed'})[job.status] || job.status;
        const progress = job.status === 'sending' && job.total_bytes ? ` · ${Math.round(job.bytes_sent / job.total_bytes * 100)}%` : '';
        const detail = job.error || `${job.device_name} · ${prettySize(job.total_bytes)}${progress}`;
        return `<div class="activity-row"><span class="rail-event-icon">${icon(job.status === 'failed' ? 'help' : 'arrow')}</span><div class="activity-row-text"><strong>${esc(job.filename)}</strong><small title="${esc(detail)}">${esc(detail)}</small></div><span class="job-badge ${esc(job.status)}">${esc(label)}</span><span class="activity-time">${esc(timeAgo(job.updated_at))}</span></div>`;
      }).join('') : `<div class="no-devices"><div class="no-device-illustration">${icon('arrow')}</div><h3>No transfers just yet.</h3><p>When you send a file to a reader, its progress will appear here.</p></div>`;
    } else {
      rows = state.events.length ? state.events.map(event => `<div class="activity-row"><span class="rail-event-icon">${icon(eventIcon(event.kind))}</span><div class="activity-row-text"><strong>${esc(event.message)}</strong><small title="${esc(event.detail)}">${esc(event.detail)}</small></div><span class="activity-time">${esc(timeAgo(event.created_at))}</span></div>`).join('') : `<div class="no-devices"><div class="no-device-illustration">${icon('clock')}</div><h3>Nothing to report just yet.</h3><p>Your library activity will find its way here.</p></div>`;
    }
    $('#activity-content').innerHTML = `<section class="content-panel activity-panel"><div class="activity-tabs"><button data-action="activity-events" class="${state.activityTab === 'events' ? 'active' : ''}">All activity</button><button data-action="activity-transfers" class="${state.activityTab === 'transfers' ? 'active' : ''}">Transfers</button></div>${rows}</section>`;
  }
  function renderSettings() {
    const s = state.status;
    if (!s) { $('#settings-content').innerHTML = '<p>Checking your settings...</p>'; return; }
    const discovery = s.wireless.discovery_ports.length ? s.wireless.discovery_ports.join(', ') : 'Unavailable';
    $('#settings-content').innerHTML = `<div class="settings-grid"><section class="content-panel settings-panel"><h2>Your library</h2><p>Give your little corner of the internet a name.</p><form id="settings-form"><label class="field-label" for="library-name">LIBRARY NAME</label><input class="text-input" id="library-name" name="library_name" maxlength="80" value="${esc(s.library_name)}" required><button class="button button-primary" type="submit">Save changes</button></form></section><section class="content-panel settings-panel"><h2>Connection details</h2><p>These settings are configured when the server starts.</p><div class="settings-line"><span>Wireless address</span><strong>${esc(wirelessAddress())}</strong></div><div class="settings-line"><span>Web / OPDS address</span><strong>${esc(s.opds_url)}</strong></div><div class="settings-line"><span>UDP discovery ports</span><strong>${esc(discovery)}</strong></div><div class="settings-line"><span>mDNS discovery</span><strong>${esc(s.wireless.mdns)}</strong></div><div class="settings-line"><span>Library location</span><strong>${esc(s.data_directory)}</strong></div></section><section class="content-panel settings-panel"><h2>Privacy &amp; access</h2><p>Keep your library on a network you trust.</p><div class="settings-line"><span>Web password</span><strong>${s.http_password_enabled ? 'Enabled' : 'Not set'}</strong></div><div class="settings-line"><span>Wireless password</span><strong>${s.wireless.password_enabled ? 'Enabled' : 'Not set'}</strong></div><div class="security-box">Connections are not encrypted. By default, anyone on your network can browse and change this library. See the README to configure passwords or change ports. Do not expose this server to the public internet.</div></section></div>`;
  }
  function switchView(view) {
    if (!['library', 'devices', 'activity', 'settings'].includes(view)) return;
    state.view = view;
    state.openMenu = null;
    $$('.view').forEach(section => { section.hidden = section.id !== `view-${view}`; });
    $$('[data-nav]').forEach(button => button.classList.toggle('active', button.dataset.nav === view && (button.classList.contains('nav-item') || !!button.closest('.mobile-nav'))));
    $('#breadcrumb-current').textContent = ({library:'Library', devices:'Devices', activity:'Activity', settings:'Settings'})[view];
    if (view === 'settings') renderSettings();
    if (view === 'library') renderLibrary();
    if (view === 'devices') renderDevices();
    if (view === 'activity') renderActivity();
    window.scrollTo({top: 0, behavior: 'smooth'});
  }
  function openModal(html) {
    $('#modal-content').innerHTML = html;
    if (!modal.open) modal.showModal();
  }
  function closeModal() { if (modal.open) modal.close(); }
  function modalHeader(kicker, title, text) {
    return `<div class="modal-top"><div><div class="section-kicker">${esc(kicker)}</div><h2 id="modal-title">${esc(title)}</h2>${text ? `<p>${esc(text)}</p>` : ''}</div><button class="icon-button" data-action="modal-close" aria-label="Close dialog">${icon('close')}</button></div>`;
  }
  function guideModal() {
    openModal(`<div class="modal-inner">${modalHeader('LET’S GET YOU CONNECTED', 'A little closer to your reader.', 'Your library is ready. Here’s how to bring your e-reader along.')}
      <ol class="steps"><li class="step"><span class="step-num">1</span><div><strong>Be on the same Wi-Fi</strong><p>Your computer running Leafline and your reader need to share a local network.</p></div></li><li class="step"><span class="step-num">2</span><div><strong>Open Calibre on your reader</strong><p>In KOReader, choose Calibre → Connect. In Calibre Companion, choose a wireless-device connection.</p></div></li><li class="step"><span class="step-num">3</span><div><strong>Find your library</strong><p>Use automatic discovery, or enter this address and port manually in your reader’s wireless settings.</p></div></li></ol><div class="copy-row"><div><small>WIRELESS ADDRESS · IP : PORT</small><strong>${esc(wirelessAddress())}</strong></div><button class="icon-button" data-action="copy-wireless" aria-label="Copy wireless address">${icon('copy')}</button></div><div class="modal-note">Your reader needs a compatible wireless client. Alternatively, add <strong>${esc(catalogAddress())}</strong> to an OPDS reader or use a web browser.</div>${cloudPreview ? `<div class="security-box"><strong>Preview note:</strong> Run Leafline on a computer on the reader’s local Wi-Fi to connect a physical device; this preview is remote.</div>` : ''}<div class="modal-actions"><button class="button button-outline" data-action="modal-close">Got it</button><button class="button button-primary" data-action="modal-devices">See devices ${icon('arrow')}</button></div></div>`);
  }
  function sendModal(ids) {
    state.modalFiles = ids.filter(id => state.files.some(file => file.id === id));
    const chosen = state.files.filter(file => state.modalFiles.includes(file.id));
    if (!chosen.length) return toast('Choose a file first.', true);
    if (!state.devices.length) {
      openModal(`<div class="modal-inner">${modalHeader('SEND TO DEVICE', 'Your reader isn’t here yet.', 'Connect a compatible reader first, then you can send files to it wirelessly.')}<div class="no-devices"><div class="no-device-illustration">${icon('device')}</div><h3>Waiting for a reader</h3><p>Make sure your reader is on the same network and start a Calibre wireless connection.</p></div><div class="modal-actions"><button class="button button-outline" data-action="modal-close">Not now</button><button class="button button-primary" data-action="modal-guide">How to connect ${icon('arrow')}</button></div></div>`);
      return;
    }
    const radios = state.devices.map((device, i) => `<label class="radio-device"><input type="radio" name="send-device" value="${esc(device.id)}" ${i === 0 ? 'checked' : ''}><span class="panel-icon">${icon('device')}</span><span><strong>${esc(device.name)}</strong><small>${esc(device.address)} · ${device.busy ? 'Busy; will be queued' : 'Connected'}</small></span></label>`).join('');
    openModal(`<div class="modal-inner">${modalHeader('SEND TO DEVICE', 'Where to next?', `Choose a connected reader for ${chosen.length === 1 ? 'your file' : `${chosen.length} files`}.`)}<div class="modal-file-list">${chosen.slice(0, 5).map(f => esc(f.filename)).join('<br>')}${chosen.length > 5 ? `<br>and ${chosen.length - 5} more...` : ''}</div><div class="radio-list">${radios}</div><div class="modal-note">Your reader decides which formats it accepts. Files with the same name on the reader may be replaced.</div><div class="modal-actions"><button class="button button-outline" data-action="modal-close">Cancel</button><button class="button button-primary" data-action="confirm-send">Send ${chosen.length === 1 ? 'file' : 'files'} ${icon('arrow')}</button></div></div>`);
  }
  function editModal(file) {
    openModal(`<div class="modal-inner">${modalHeader('A LITTLE EDIT', 'Make it your own.', 'Change the details shown in your library. The original file stays the same.')}<label class="field-label modal-label" for="edit-title">TITLE</label><input class="text-input" id="edit-title" maxlength="300" value="${esc(file.title)}"><label class="field-label modal-label" for="edit-author">AUTHOR / CREATOR</label><input class="text-input" id="edit-author" maxlength="300" value="${esc(file.author)}"><div class="modal-actions"><button class="button button-outline" data-action="modal-close">Cancel</button><button class="button button-primary" data-action="confirm-edit" data-id="${esc(file.id)}">Save details</button></div></div>`);
  }
  function deleteModal(file) {
    openModal(`<div class="modal-inner">${modalHeader('REMOVE FROM LIBRARY', 'Ready to say goodbye?', `“${file.filename}” will be removed from this library. This won’t delete copies already on your reader.`)}<div class="modal-actions"><button class="button button-outline" data-action="modal-close">Keep file</button><button class="button button-danger" data-action="confirm-delete" data-id="${esc(file.id)}">Remove file</button></div></div>`);
  }
  function disconnectModal(device) {
    openModal(`<div class="modal-inner">${modalHeader('DISCONNECT READER', 'Until next time.', `Disconnect ${device.name}? Transfers already in the queue will finish first.`)}<div class="modal-actions"><button class="button button-outline" data-action="modal-close">Cancel</button><button class="button button-primary" data-action="confirm-disconnect" data-id="${esc(device.id)}">Disconnect</button></div></div>`);
  }
  async function copyText(text) {
    if (!text || text === 'Checking address...') return toast('The address is not ready yet.', true);
    try {
      if (navigator.clipboard && window.isSecureContext) await navigator.clipboard.writeText(text);
      else {
        const field = document.createElement('textarea');
        field.value = text; field.style.position = 'fixed'; field.style.opacity = '0';
        document.body.append(field); field.select();
        if (!document.execCommand('copy')) throw new Error('Copy failed');
        field.remove();
      }
      toast('Copied to clipboard.');
    } catch (_) { toast(`Could not copy. Address: ${text}`, true); }
  }
  function uploadFiles(files) {
    if (!files.length) return;
    const data = new FormData();
    [...files].forEach(file => data.append('files', file, file.name));
    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/files');
    xhr.setRequestHeader('X-Requested-With', 'XMLHttpRequest');
    $('#upload-progress').hidden = false;
    $('#upload-label').textContent = `Adding ${files.length === 1 ? 'your file' : `${files.length} files`}...`;
    $('#upload-percent').textContent = '0%';
    $('#upload-bar').style.width = '0%';
    xhr.upload.onprogress = event => {
      if (event.lengthComputable) {
        const percent = Math.round(event.loaded / event.total * 100);
        $('#upload-percent').textContent = `${percent}%`;
        $('#upload-bar').style.width = `${percent}%`;
        if (percent === 100) $('#upload-label').textContent = 'Making room in your library...';
      }
    };
    xhr.onload = () => {
      $('#upload-progress').hidden = true;
      let result = {};
      try { result = JSON.parse(xhr.responseText); } catch (_) { /* generic error below */ }
      if (xhr.status >= 200 && xhr.status < 300) {
        const count = result.files?.length || 0;
        toast(`${count} ${count === 1 ? 'file' : 'files'} added to your library.${result.errors?.length ? ` ${result.errors.length} could not be added.` : ''}`);
        state.filter = 'all'; state.query = ''; $('#library-search').value = '';
        switchView('library'); refresh();
      } else toast(result.error || 'Your files could not be added.', true);
    };
    xhr.onerror = () => { $('#upload-progress').hidden = true; toast('Upload interrupted. Please try again.', true); };
    xhr.send(data);
  }

  document.addEventListener('click', async event => {
    const nav = event.target.closest('[data-nav]');
    if (nav) { closeModal(); switchView(nav.dataset.nav); return; }
    const filter = event.target.closest('[data-filter]');
    if (filter) { state.filter = filter.dataset.filter; state.openMenu = null; renderLibrary(); return; }
    const target = event.target.closest('[data-action]');
    if (!target) {
      if (state.openMenu && !event.target.closest('.file-menu')) { state.openMenu = null; renderLibrary(); }
      return;
    }
    const action = target.dataset.action;
    const id = target.dataset.id;
    const file = state.files.find(f => f.id === id);
    try {
      switch (action) {
        case 'upload': fileInput.click(); break;
        case 'sample': {
          const sample = new File([`Welcome to Leafline!\n\nThis little file is here to help you try your new library.\n\nTo send it to your reader:\n1. Put your reader and this server on the same Wi-Fi.\n2. Connect using a Calibre wireless-device client, such as KOReader.\n3. Choose this file in your library and select Send to device.\n\nYou can also browse the OPDS catalog or download files from the web app.\n\nHappy reading!\n`], 'Welcome to Leafline.txt', {type:'text/plain'});
          uploadFiles([sample]); break;
        }
        case 'guide': case 'modal-guide': guideModal(); break;
        case 'modal-close': closeModal(); break;
        case 'modal-devices': closeModal(); switchView('devices'); break;
        case 'copy-wireless': await copyText(wirelessAddress()); break;
        case 'copy-opds': await copyText(catalogAddress()); break;
        case 'view-grid': state.display = 'grid'; state.openMenu = null; renderLibrary(); break;
        case 'view-list': state.display = 'list'; state.openMenu = null; renderLibrary(); break;
        case 'reset-filter': state.filter = 'all'; state.query = ''; $('#library-search').value = ''; renderLibrary(); break;
        case 'clear-selection': state.selected.clear(); renderLibrary(); break;
        case 'send-selected': sendModal([...state.selected]); break;
        case 'menu': state.openMenu = state.openMenu === id ? null : id; renderLibrary(); break;
        case 'send-file': if (file) { state.openMenu = null; renderLibrary(); sendModal([id]); } break;
        case 'edit-file': if (file) { state.openMenu = null; renderLibrary(); editModal(file); } break;
        case 'delete-file': if (file) { state.openMenu = null; renderLibrary(); deleteModal(file); } break;
        case 'confirm-edit': {
          const title = $('#edit-title').value.trim();
          if (!title) { toast('Please add a title.', true); break; }
          await api(`/api/files/${encodeURIComponent(id)}`, {method:'PATCH', json:{title, author:$('#edit-author').value}});
          closeModal(); toast('Your details are saved.'); await refresh(); break;
        }
        case 'confirm-delete': await api(`/api/files/${encodeURIComponent(id)}`, {method:'DELETE'}); closeModal(); state.selected.delete(id); toast('File removed from your library.'); await refresh(); break;
        case 'confirm-send': {
          const radio = $('input[name="send-device"]:checked', modal);
          if (!radio) { toast('Choose a reader first.', true); break; }
          const result = await api('/api/transfers', {method:'POST', json:{device_id:radio.value, file_ids:state.modalFiles}});
          closeModal(); state.selected.clear();
          const count = result.queued.length;
          toast(`${count} ${count === 1 ? 'file' : 'files'} queued for your reader.${result.rejected.length ? ` ${result.rejected.length} not supported by this reader.` : ''}`, !!result.rejected.length);
          await refresh(); break;
        }
        case 'disconnect': { const device = state.devices.find(d => d.id === id); if (device) disconnectModal(device); break; }
        case 'confirm-disconnect': await api(`/api/devices/${encodeURIComponent(id)}/disconnect`, {method:'POST'}); closeModal(); toast('Disconnecting your reader.'); await refresh(); break;
        case 'activity-events': state.activityTab = 'events'; renderActivity(); break;
        case 'activity-transfers': state.activityTab = 'transfers'; renderActivity(); break;
      }
    } catch (error) {
      const rejected = error.data?.rejected;
      toast(rejected?.length ? rejected.map(r => `${r.filename}: ${r.message}`).join(' ') : error.message, true);
      await refresh();
    }
  });
  document.addEventListener('change', event => {
    if (event.target.matches('[data-select]')) {
      if (event.target.checked) state.selected.add(event.target.dataset.select);
      else state.selected.delete(event.target.dataset.select);
      renderLibrary();
    }
  });
  document.addEventListener('submit', async event => {
    if (event.target.id !== 'settings-form') return;
    event.preventDefault();
    const form = event.target;
    if (form.dataset.saving) return;
    form.dataset.saving = 'true';
    const input = $('#library-name', form);
    const submit = $('button[type="submit"]', form);
    const name = input.value;
    input.disabled = true;
    submit.disabled = true;
    try {
      const saved = await api('/api/settings', {method:'PUT', json:{library_name:name}});
      // Update immediately even if a periodic refresh was already in flight.
      if (state.status) state.status.library_name = saved.library_name;
      toast('Your library name is saved.');
      if (state.view === 'settings') renderSettings();
      await refresh();
    } catch (error) {
      input.disabled = false;
      submit.disabled = false;
      delete form.dataset.saving;
      toast(error.message, true);
    }
  });
  fileInput.addEventListener('change', event => { const files = [...event.target.files]; event.target.value = ''; uploadFiles(files); });
  $('#library-search').addEventListener('input', event => { state.query = event.target.value; state.openMenu = null; renderLibrary(); });
  modal.addEventListener('click', event => { if (event.target === modal) closeModal(); });
  let dragDepth = 0;
  document.addEventListener('dragenter', event => {
    if (!event.dataTransfer?.types.includes('Files')) return;
    event.preventDefault(); dragDepth++; $('#drop-overlay').hidden = false;
  });
  document.addEventListener('dragover', event => { if (event.dataTransfer?.types.includes('Files')) event.preventDefault(); });
  document.addEventListener('dragleave', event => { if (!event.dataTransfer?.types.includes('Files')) return; dragDepth = Math.max(0, dragDepth - 1); if (!dragDepth) $('#drop-overlay').hidden = true; });
  document.addEventListener('drop', event => {
    if (!event.dataTransfer?.files.length) return;
    event.preventDefault(); dragDepth = 0; $('#drop-overlay').hidden = true; uploadFiles([...event.dataTransfer.files]);
  });
  renderLibrary();
  refresh();
  setInterval(refresh, 4000);
})();
