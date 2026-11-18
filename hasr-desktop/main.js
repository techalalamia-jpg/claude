const { app, BrowserWindow, Menu, shell, net } = require('electron');
const path = require('path');
const fs = require('fs');

const UPDATE_URL = 'https://github.com/techalalamia-jpg/claude/releases/download/hasr-web-latest/index.html';
const bundled = path.join(__dirname, 'app', 'index.html');
const cached = () => path.join(app.getPath('userData'), 'index.html');

if (!app.requestSingleInstanceLock()) app.quit();

async function refreshCache() {
  try {
    const ctl = new AbortController();
    const to = setTimeout(() => ctl.abort(), 5000);
    const r = await net.fetch(UPDATE_URL, { signal: ctl.signal });
    clearTimeout(to);
    if (!r.ok) return;
    const txt = await r.text();
    if (txt.length > 200000 && txt.includes('</html>')) fs.writeFileSync(cached(), txt, 'utf8');
  } catch (e) { /* offline: use cache or bundled copy */ }
}

async function createWindow() {
  await refreshCache();
  const win = new BrowserWindow({
    width: 1400, height: 900, show: false, autoHideMenuBar: true,
    title: 'المكتب الفني', icon: path.join(__dirname, 'build', 'icon.ico'),
    webPreferences: { contextIsolation: true, spellcheck: false }
  });
  Menu.setApplicationMenu(null);
  win.maximize();
  win.once('ready-to-show', () => win.show());
  win.webContents.setWindowOpenHandler(({ url }) => { shell.openExternal(url); return { action: 'deny' }; });
  win.webContents.on('will-navigate', (e, url) => { if (!url.startsWith('file:')) { e.preventDefault(); shell.openExternal(url); } });
  const f = fs.existsSync(cached()) ? cached() : bundled;
  win.loadFile(f).catch(() => win.loadFile(bundled));
  win.webContents.on('before-input-event', (e, i) => {
    if (i.type === 'keyDown' && (i.key === 'F5' || (i.control && i.key.toLowerCase() === 'r'))) { win.reload(); e.preventDefault(); }
    if (i.type === 'keyDown' && i.key === 'F11') { win.setFullScreen(!win.isFullScreen()); e.preventDefault(); }
  });
}

app.whenReady().then(createWindow);
app.on('window-all-closed', () => app.quit());
