// Generic table sort. Add class="sortable" to a <table> and class="sortable-col"
// to each <th> that should be sortable. Optionally data-col on <th> for label.
(function() {
    function compareCells(a, b, dir) {
        var av = a.textContent.trim();
        var bv = b.textContent.trim();
        // Try numeric (handles "5d", "+12d", "23")
        var an = parseFloat(av.replace(/[^\d.\-]/g, ''));
        var bn = parseFloat(bv.replace(/[^\d.\-]/g, ''));
        if (!isNaN(an) && !isNaN(bn) && (av.match(/^[\-\+]?\d/) || bv.match(/^[\-\+]?\d/))) {
            return dir * (an - bn);
        }
        // Try date (Mon DD format)
        var months = {Jan:1,Feb:2,Mar:3,Apr:4,May:5,Jun:6,Jul:7,Aug:8,Sep:9,Oct:10,Nov:11,Dec:12};
        var am = av.match(/^([A-Z][a-z]{2})\s+(\d+)/);
        var bm = bv.match(/^([A-Z][a-z]{2})\s+(\d+)/);
        if (am && bm) {
            return dir * ((months[am[1]] * 100 + parseInt(am[2])) - (months[bm[1]] * 100 + parseInt(bm[2])));
        }
        // String compare
        return dir * av.localeCompare(bv);
    }

    function sortTable(table, colIndex, ascending) {
        var tbody = table.querySelector('tbody');
        if (!tbody) return;
        var rows = Array.prototype.slice.call(tbody.querySelectorAll('tr'));
        var dir = ascending ? 1 : -1;
        rows.sort(function(r1, r2) {
            var c1 = r1.children[colIndex];
            var c2 = r2.children[colIndex];
            if (!c1 || !c2) return 0;
            return compareCells(c1, c2, dir);
        });
        rows.forEach(function(r) { tbody.appendChild(r); });
    }

    function attachSortHandlers(table) {
        var headers = table.querySelectorAll('th');
        headers.forEach(function(th, idx) {
            // Auto-mark all headers as sortable unless they explicitly opt out
            if (!th.classList.contains('no-sort') && !th.classList.contains('sortable-col')) {
                th.classList.add('sortable-col');
            }
            if (th.classList.contains('no-sort')) return;
            th.addEventListener('click', function() {
                var ascending = !th.classList.contains('sort-asc');
                table.querySelectorAll('th').forEach(function(h) {
                    h.classList.remove('sort-asc', 'sort-desc');
                });
                th.classList.add(ascending ? 'sort-asc' : 'sort-desc');
                sortTable(table, idx, ascending);
            });
        });
    }

    window.attachSorting = function(root) {
        var tables = (root || document).querySelectorAll('table.sortable');
        tables.forEach(attachSortHandlers);
    };
})();
