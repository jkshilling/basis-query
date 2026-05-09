// Common UI bits used across pages.

// Show "Data refreshed N min ago" in the footer.
(function() {
    function pluralize(n, s) { return n + ' ' + s + (n === 1 ? '' : 's'); }

    function formatAge(seconds) {
        if (seconds === undefined) return '';
        if (seconds < 60) return 'just now';
        var m = Math.round(seconds / 60);
        if (m < 60) return pluralize(m, 'minute') + ' ago';
        var h = Math.round(m / 60);
        if (h < 24) return pluralize(h, 'hour') + ' ago';
        var d = Math.round(h / 24);
        return pluralize(d, 'day') + ' ago';
    }

    function showFreshness() {
        var footer = document.querySelector('footer');
        if (!footer) return;
        // Pick the cache key that's most relevant to the current page.
        fetch('/api/freshness').then(function(r) { return r.json(); }).then(function(data) {
            // Use dashboard_stats as a proxy for "most recent refresh"
            var keys = ['dashboard_stats', 'all_actions'];
            var minAge = null;
            keys.forEach(function(k) {
                if (data[k] !== undefined && (minAge === null || data[k] < minAge)) {
                    minAge = data[k];
                }
            });
            if (minAge === null) return;
            var note = document.createElement('div');
            note.className = 'freshness';
            note.textContent = 'Data refreshed ' + formatAge(minAge);
            footer.appendChild(note);
        }).catch(function() {});
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', showFreshness);
    } else {
        showFreshness();
    }
})();
