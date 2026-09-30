'use strict';

document.addEventListener('DOMContentLoaded', () => {
    const form = document.getElementById('login-form');
    const err = document.getElementById('login-error');
    form.addEventListener('submit', async (e) => {
        e.preventDefault();
        err.textContent = '';
        const btn = form.querySelector('button');
        btn.disabled = true;
        try {
            const res = await fetch('/api/v1/auth/login', {
                method: 'POST',
                credentials: 'same-origin',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    username: document.getElementById('username').value.trim(),
                    password: document.getElementById('password').value,
                }),
            });
            if (res.ok) {
                window.location.replace('/');
                return;
            }
            const body = await res.json().catch(() => ({}));
            err.textContent = typeof body.detail === 'string' ? body.detail : 'Sign-in failed';
        } catch (_) {
            err.textContent = 'Network error';
        } finally {
            document.getElementById('password').value = '';
            btn.disabled = false;
        }
    });
});
