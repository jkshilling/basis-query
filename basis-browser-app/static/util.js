// Small helpers shared across templates. Loaded synchronously before any
// inline scripts that use them.

// HTML-escape a string. Returns '' for null/undefined.
function esc(s) {
    if (s === undefined || s === null) return '';
    var d = document.createElement('div');
    d.textContent = String(s);
    return d.innerHTML;
}

// Render a chamber tag with consistent color.
//   chamberSpan('H')  -> '<span class="chamber-h">(H)</span>'
//   chamberSpan('S')  -> '<span class="chamber-s">(S)</span>'
//   chamberSpan('')   -> '' (or whatever falsy was passed)
function chamberSpan(c) {
    if (c === 'H') return '<span class="chamber-h">(H)</span>';
    if (c === 'S') return '<span class="chamber-s">(S)</span>';
    return '';
}

// Render a clickable bill number link.
function billLink(billnumber) {
    return '<a href="/bill/' + encodeURIComponent(billnumber) + '">' + esc(billnumber) + '</a>';
}

// Render a clickable committee link with chamber prefix.
function committeeLink(chamber, code) {
    return chamberSpan(chamber) + ' <a class="cmte-link" href="/committee/' +
        encodeURIComponent(chamber) + '/' + encodeURIComponent(code) + '">' +
        esc(code) + '</a>';
}
