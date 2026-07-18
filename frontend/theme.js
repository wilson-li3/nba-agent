/* Theme picker — palette button + popover, persisted in localStorage.
   Colors switch live because every color routes through CSS vars. */

const BDL_THEMES = [
  { id: '',         label: 'Graphite', swatch: ['oklch(0.165 0.012 265)', 'oklch(0.72 0.15 55)'] },
  { id: 'midnight', label: 'Midnight', swatch: ['oklch(0.16 0.03 290)',  'oklch(0.72 0.16 305)'] },
  { id: 'emerald',  label: 'Emerald',  swatch: ['oklch(0.155 0.018 175)', 'oklch(0.77 0.13 162)'] },
  { id: 'daylight', label: 'Daylight', swatch: ['oklch(0.985 0.002 80)', 'oklch(0.62 0.145 55)'] },
];

function bdlSetTheme(id) {
  document.documentElement.dataset.theme = id;
  try { localStorage.setItem('bdl_theme', id); } catch (e) { /* private mode */ }
  document.querySelectorAll('.theme-option').forEach(el => {
    el.classList.toggle('active', el.dataset.themeId === id);
  });
}

function initThemePicker(mount) {
  const current = document.documentElement.dataset.theme || '';

  const btn = document.createElement('button');
  btn.className = 'theme-btn';
  btn.title = 'Color theme';
  btn.setAttribute('aria-label', 'Color theme');
  btn.setAttribute('popovertarget', 'themePopover');
  btn.innerHTML = '<iconify-icon icon="tabler:palette"></iconify-icon>';
  mount.appendChild(btn);

  const pop = document.createElement('div');
  pop.id = 'themePopover';
  pop.setAttribute('popover', '');
  pop.innerHTML = BDL_THEMES.map(t => `
    <button class="theme-option ${t.id === current ? 'active' : ''}" data-theme-id="${t.id}"
      onclick="bdlSetTheme('${t.id}')">
      <span class="theme-swatch">
        <span style="background:${t.swatch[0]}"></span>
        <span style="background:${t.swatch[1]}"></span>
      </span>
      <span class="theme-name">${t.label}</span>
      <iconify-icon class="theme-check" icon="tabler:check"></iconify-icon>
    </button>`).join('');
  document.body.appendChild(pop);
}
