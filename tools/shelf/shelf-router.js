// CloudFront Function `shelf-router` (viewer-request, cloudfront-js-2.0) on the
// DEFAULT behaviour of the reference shelf's distribution (reference.aaronhaspel.com).
//
// The shelf holds several books, each under its own prefix (/dnb/, /century/, …),
// each a copy of the engine's site.  CloudFront's root object applies only at "/",
// so a book's own front door and its article URLs are routed here:
//
//   /                      -> /index.html            (the shelf's landing page)
//   /{book} , /{book}/     -> /{book}/home.html      (the book's front page)
//   /{book}/article/...    -> /{book}/viewer.html    (the client renders the article)
//
// Anything else is served as it is.  The prefix is any one path segment: a book is
// added to the shelf by deploying it, never by editing this function.  Source of
// truth for the deployed function; tools/shelf/setup_shelf.py publishes it.
var BOOK_ROOT = /^\/([a-z0-9-]+)\/?$/;
var ARTICLE = /^\/([a-z0-9-]+)\/article\//;

function handler(event) {
    var request = event.request;
    var uri = request.uri;
    if (uri === '/') {
        request.uri = '/index.html';
        return request;
    }
    var m = uri.match(BOOK_ROOT);
    if (m) {
        request.uri = '/' + m[1] + '/home.html';
        return request;
    }
    m = uri.match(ARTICLE);
    if (m) {
        request.uri = '/' + m[1] + '/viewer.html';
    }
    return request;
}
