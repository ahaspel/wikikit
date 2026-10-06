// CloudFront Function `shelf-search` (viewer-request, cloudfront-js-2.0) on the
// /*/search-api/* behaviour of the reference shelf's distribution.
//
// Each book's pages call /{book}/search-api/…, forwarded to the ONE Meilisearch
// host the Britannica already uses (one index per book).  As there
// (britannica-edition/tools/cloudfront/strip-search-prefix.js) this is an
// ALLOW-LIST, never a pass-through: the only calls that reach the server are a
// search on one of the shelf's own indexes and the health probe.  Every admin
// endpoint, and every other index — the Britannica's `articles` included — is
// refused here, so no key, leaked or not, gets past the edge.
//
// INDEXES is filled in by tools/shelf/setup_shelf.py from shelf.json; this file is
// the source of truth for everything else.
var INDEXES = __INDEXES__;
var PREFIX = /^\/([a-z0-9-]+)\/search-api(\/.*)?$/;

function forbidden() {
    return {statusCode: 403, statusDescription: 'Forbidden',
            headers: {'cache-control': {value: 'no-store'}}};
}

function handler(event) {
    var request = event.request;
    var m = request.uri.match(PREFIX);
    if (!m) {
        return forbidden();
    }
    var book = m[1];
    var uri = m[2] || '/';
    var method = request.method;
    var index = INDEXES[book];
    var allowed =
        (index && uri === '/indexes/' + index + '/search' && (method === 'POST' || method === 'OPTIONS')) ||
        (uri === '/health' && (method === 'GET' || method === 'HEAD'));
    if (!allowed) {
        return forbidden();
    }
    request.uri = uri;
    return request;
}
