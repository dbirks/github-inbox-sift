// Global shortcuts (design-handoff §10). Disabled while typing and when Cmd/Ctrl/Alt is held, except Esc.

import { isEditable } from './dom.js';
import { LIST_VIEWS, currentId, pickCount } from './actions.js';

export function installKeyboard(store, actions) {
  window.addEventListener('keydown', e => {
    const s = store.get();
    if (e.key === 'Escape') {
      // Close the FIRST that applies: shortcut sheet -> menu/popover -> confirm dialog -> selection -> input -> panel.
      if (s.help) return actions.toggleHelp();
      if (s.openMenu) return actions.closeMenus();
      if (s.bulkConfirm) return actions.cancelBulk();
      if (s.picked.size || s.pickAll) return actions.clearPicks();
      if (isEditable(e.target)) { e.target.blur(); return; }
      if (s.panelOpen) return actions.closePanel();
      return;
    }
    if (isEditable(e.target) || e.metaKey || e.ctrlKey || e.altKey) return;
    if (s.bulkConfirm || s.help) return; // dialogs own the keyboard (they handle Enter themselves)
    const inList = LIST_VIEWS.includes(s.view);
    let handled = true;
    switch (e.key) {
      case '?': actions.toggleHelp(); break;
      case '[': actions.toggleNav(); break;
      case '1': actions.go('inbox'); break;
      case '2': actions.go('filtered'); break;
      case '3': actions.go('all'); break;
      case '/': {
        const el = document.querySelector('[data-testid="search"]');
        if (el) el.focus(); else handled = false;
        break;
      }
      default: handled = false;
    }
    if (!handled && inList) {
      handled = true;
      switch (e.key) {
        case 'j': actions.moveSel(1); break;
        case 'k': actions.moveSel(-1); break;
        case 'J': actions.extendPickBy(1); break;
        case 'K': actions.extendPickBy(-1); break;
        case 'x': actions.togglePick(currentId(s)); break;
        case 'Enter':
          // Let focused buttons/links handle their own Enter.
          if (e.target && e.target.closest && e.target.closest('button, a, [role="menuitem"]')) { handled = false; break; }
          actions.openPanel(); break;
        case 'e': if (pickCount(s)) actions.doneSelection(); else actions.markDone(); break;
        case 'r': actions.toggleRead(); break;
        case 'o': actions.openOnGitHub(); break;
        default: handled = false;
      }
    }
    if (handled) e.preventDefault();
  });
}
