// Renderer-local enum preferences only. No credentials or research state.
(() => {
  const key = 'tessera.appearance.v1';
  const modes = ['system', 'light', 'dark'];
  const palettes = ['plum', 'lagoon', 'graphite'];
  let preference = { mode: 'system', palette: 'plum' };
  let storageAvailable = true;
  try {
    const saved = JSON.parse(localStorage.getItem(key));
    if (saved && modes.includes(saved.mode) && palettes.includes(saved.palette)) {
      preference = { mode: saved.mode, palette: saved.palette };
    }
  } catch { storageAvailable = false; }
  const scheme = matchMedia('(prefers-color-scheme: dark)');
  const apply = () => {
    document.documentElement.dataset.theme = preference.mode === 'system' ? (scheme.matches ? 'dark' : 'light') : preference.mode;
    document.documentElement.dataset.palette = preference.palette;
  };
  apply();
  scheme.addEventListener('change', apply);
  document.addEventListener('DOMContentLoaded', () => {
    const get = id => document.getElementById(id);
    const dialog = get('appearance-dialog');
    const mode = get('appearance-mode');
    const palette = get('appearance-palette');
    const status = () => { get('appearance-status').textContent = storageAvailable ? 'Saved automatically on this desktop.' : 'Storage unavailable. Appearance applies until this window reloads.'; };
    get('appearance-open').addEventListener('click', () => {
      mode.value = preference.mode; palette.value = preference.palette; status(); dialog.showModal();
    });
    for (const control of [mode, palette]) control.addEventListener('change', () => {
      if (!modes.includes(mode.value) || !palettes.includes(palette.value)) return;
      preference = { mode: mode.value, palette: palette.value }; apply();
      try { localStorage.setItem(key, JSON.stringify(preference)); storageAvailable = true; }
      catch { storageAvailable = false; }
      status();
    });
    dialog.addEventListener('close', () => get('appearance-open').focus());
  });
})();
