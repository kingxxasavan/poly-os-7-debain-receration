// Win+P, like Windows' "Project": the same picture on every screen, one desktop across them, or one screen.

import { api, closePopup } from '../api.js';
import { h, icon } from '../ui.js';

const MODES = [
  ['duplicate', 'Duplicate', 'The same picture on every screen'],
  ['extend', 'Extend', 'One desktop across your screens'],
  ['main', 'This screen only', 'The other screens stay off'],
  ['second', 'Second screen only', 'Only the screen you plugged in'],
];

export default function project(root, store) {
  root.classList.add('project');
  const note = h('p.pj-note', { role: 'status' });
  const choose = async (mode) => {
    note.textContent = 'Setting up your screens…';
    try {
      await api.post('/api/displays/mode', { mode });
      closePopup();
    } catch (err) {
      note.textContent = err.message;
    }
  };
  root.append(h('div.pj-head', icon('monitor'), h('b', 'Project')),
    h('div.pj-list', MODES.map(([mode, name, sub], i) => h('button.pj-item', {
      class: store.state.settings.displayMode === mode ? 'on' : '', autofocus: i === 0, onclick: () => choose(mode),
    }, h('span.pj-ico', { class: `pj-${mode}` }, h('i'), h('i')), h('span.pj-text', h('b', name), h('small', sub))))),
    note);
}
