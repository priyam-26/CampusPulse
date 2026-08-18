// ─── Custom confirm dialog (replaces window.confirm) ────────────────────────
const confirmOverlay = document.getElementById('confirmOverlay');
const confirmTitleEl = document.getElementById('confirmTitle');
const confirmMessageEl = document.getElementById('confirmMessage');
const confirmIconEl = document.getElementById('confirmIcon');
const confirmOkBtn = document.getElementById('confirmOkBtn');
const confirmCancelBtn = document.getElementById('confirmCancelBtn');
let confirmResolve = null;

function showConfirm({ title = 'Are you sure?', message = '', confirmText = 'Confirm', icon = '⚠️' } = {}) {
  if (!confirmOverlay) return Promise.resolve(true); // graceful fallback if markup is missing
  confirmTitleEl.textContent = title;
  confirmMessageEl.textContent = message;
  confirmIconEl.textContent = icon;
  confirmOkBtn.textContent = confirmText;
  confirmOverlay.classList.add('open');
  confirmOkBtn.focus();
  return new Promise(resolve => { confirmResolve = resolve; });
}

function closeConfirm(result) {
  confirmOverlay.classList.remove('open');
  if (confirmResolve) { confirmResolve(result); confirmResolve = null; }
}

if (confirmOverlay) {
  confirmOkBtn.addEventListener('click', () => closeConfirm(true));
  confirmCancelBtn.addEventListener('click', () => closeConfirm(false));
  // Click on the dimmed backdrop cancels, same as pressing Escape
  confirmOverlay.addEventListener('click', (e) => {
    if (e.target === confirmOverlay) closeConfirm(false);
  });
  document.addEventListener('keydown', (e) => {
    if (!confirmOverlay.classList.contains('open')) return;
    if (e.key === 'Escape') closeConfirm(false);
    if (e.key === 'Enter') closeConfirm(true);
  });
}

// Any form marked with data-confirm-message gets intercepted and routed
// through the themed dialog above instead of the browser's blocking
// window.confirm(). Once approved, the form is re-submitted for real.
document.querySelectorAll('form[data-confirm-message]').forEach(form => {
  form.addEventListener('submit', async (e) => {
    if (form.dataset.confirmed === 'true') return; // already approved — let it through
    e.preventDefault();
    const approved = await showConfirm({
      title: form.dataset.confirmTitle || 'Are you sure?',
      message: form.dataset.confirmMessage,
      confirmText: form.dataset.confirmConfirmText || 'Confirm',
      icon: form.dataset.confirmIcon || '⚠️',
    });
    if (approved) {
      form.dataset.confirmed = 'true';
      form.requestSubmit ? form.requestSubmit() : form.submit();
    }
  });
});

// ─── Toast notifications (replaces window.alert) ────────────────────────────
function showToast(message, type = 'success', duration = 3000) {
  const stack = document.getElementById('toastStack');
  if (!stack) { return; }
  const toast = document.createElement('div');
  toast.className = `toast toast-${type}`;
  toast.textContent = message;
  stack.appendChild(toast);
  requestAnimationFrame(() => toast.classList.add('show'));
  setTimeout(() => {
    toast.classList.remove('show');
    setTimeout(() => toast.remove(), 250);
  }, duration);
}

// Mobile nav toggle
const navToggle = document.getElementById('navToggle');
const navLinks = document.getElementById('navLinks');
if (navToggle && navLinks) {
  navToggle.addEventListener('click', () => {
    const isOpen = navLinks.classList.toggle('open');
    navToggle.classList.toggle('open', isOpen);
    navToggle.setAttribute('aria-expanded', isOpen ? 'true' : 'false');
  });
  // Close menu when a link is tapped
  navLinks.querySelectorAll('a').forEach(link => {
    link.addEventListener('click', () => {
      navLinks.classList.remove('open');
      navToggle.classList.remove('open');
      navToggle.setAttribute('aria-expanded', 'false');
    });
  });
}

// Animate numbers on admin dashboard
document.querySelectorAll('.stat-num').forEach(el => {
  const target = parseInt(el.textContent.replace(/[^0-9]/g, ''));
  if (isNaN(target) || target === 0) return;
  let current = 0;
  const step = Math.ceil(target / 40);
  const interval = setInterval(() => {
    current = Math.min(current + step, target);
    const suffix = el.textContent.includes('%') ? '%' : '';
    el.textContent = current + suffix;
    if (current >= target) clearInterval(interval);
  }, 20);
});

// Progress bar width animation
document.querySelectorAll('.progress-fill').forEach(el => {
  const w = el.style.width;
  el.style.width = '0';
  setTimeout(() => { el.style.transition = 'width 0.8s ease'; el.style.width = w; }, 100);
});

// Form input focus effects
document.querySelectorAll('input, textarea').forEach(input => {
  input.addEventListener('focus', () => {
    input.parentElement?.classList.add('focused');
  });
  input.addEventListener('blur', () => {
    input.parentElement?.classList.remove('focused');
  });
});

// Auto dismiss alerts
const alerts = document.querySelectorAll('.alert');
alerts.forEach(alert => {
  setTimeout(() => {
    alert.style.transition = 'opacity 0.5s';
    alert.style.opacity = '0';
    setTimeout(() => alert.remove(), 500);
  }, 5000);
});