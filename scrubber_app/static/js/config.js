/* config.js — API base URL derived from the current page URL */
const _u = new URL(window.location.href);
const _path = _u.pathname.endsWith('/') ? _u.pathname : _u.pathname + '/';
const API_BASE = _u.origin + _path;
