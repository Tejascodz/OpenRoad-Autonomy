'use strict';
// Small fetch wrapper: same-origin cookies + CSRF header on unsafe methods,
// redirect to /login on 401. Never builds HTML from server data.

const Api = (() => {
    let csrfToken = null;

    function readCookie(name) {
        const m = document.cookie.split('; ').find((c) => c.startsWith(name + '='));
        return m ? decodeURIComponent(m.split('=')[1]) : null;
    }

    async function request(method, url, body, isForm) {
        const headers = {};
        const opts = { method, credentials: 'same-origin', headers };
        if (method !== 'GET') {
            headers['X-CSRF-Token'] = csrfToken || readCookie('or_csrf') || '';
            if (body !== undefined) {
                if (isForm) {
                    opts.body = body;
                } else {
                    headers['Content-Type'] = 'application/json';
                    opts.body = JSON.stringify(body);
                }
            }
        }
        const res = await fetch(url, opts);
        if (res.status === 401) {
            window.location.replace('/login');
            throw new Error('Not authenticated');
        }
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
            let msg = 'Request failed (' + res.status + ')';
            if (typeof data.detail === 'string') msg = data.detail;
            else if (Array.isArray(data.detail) && data.detail.length) msg = data.detail[0].msg;
            const err = new Error(msg);
            err.status = res.status;
            throw err;
        }
        return data;
    }

    return {
        setCsrf: (t) => { csrfToken = t; },
        get: (u) => request('GET', u),
        post: (u, b) => request('POST', u, b),
        patch: (u, b) => request('PATCH', u, b),
        del: (u) => request('DELETE', u),
        upload: (u, formData) => request('POST', u, formData, true),
    };
})();
