(function() {
	var input = document.getElementById("search-input");
	var results = document.getElementById("search-results");
	if (!input || !results) return;

	var index = null;
	var docs = null;

	function loadIndex() {
		if (docs) return Promise.resolve();
		return fetch(window.BDG_SEARCH_JSON).then(function(r) { return r.json(); }).then(function(data) {
			docs = data.docs;
			index = new MiniSearch({
				fields: ["name", "qname", "brief"],
				storeFields: ["name", "qname", "kind", "url", "brief", "isInternal"],
				searchOptions: { prefix: true, fuzzy: 0.15, boost: { name: 2, qname: 1.5 } }
			});
			index.addAll(docs.map(function(d, i) { return Object.assign({ id: i }, d); }));
		});
	}

	function showInternal() {
		return document.documentElement.classList.contains("show-internal");
	}

	function render(matches) {
		if (!matches.length) { results.hidden = true; results.innerHTML = ""; return; }
		var html = "";
		var shown = 0;
		for (var i = 0; i < matches.length && shown < 30; i++) {
			var m = matches[i];
			if (m.isInternal && !showInternal()) continue;
			var cls = "search-hit" + (m.isInternal ? " internal" : "");
			html += '<a class="' + cls + '" href="' + window.BDG_BASE + m.url + '">';
			html += '<span class="kind">' + m.kind + '</span>';
			html += '<strong>' + m.qname + '</strong>';
			if (m.brief) html += '<span class="brief"> — ' + m.brief + '</span>';
			html += '</a>';
			shown++;
		}
		results.innerHTML = html;
		results.hidden = shown === 0;
	}

	var timer = null;
	input.addEventListener("input", function() {
		if (timer) clearTimeout(timer);
		var q = input.value.trim();
		if (!q) { results.hidden = true; results.innerHTML = ""; return; }
		timer = setTimeout(function() {
			loadIndex().then(function() {
				render(index.search(q));
			});
		}, 80);
	});

	document.addEventListener("click", function(e) {
		if (!results.contains(e.target) && e.target !== input) {
			results.hidden = true;
		}
	});
})();
