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
