(function() {
	var KEY = "bansheedocgenerator.settings.v1";
	var defaults = { showInternal: false };
	function load() {
		try { return Object.assign({}, defaults, JSON.parse(localStorage.getItem(KEY)) || {}); }
		catch (e) { return Object.assign({}, defaults); }
	}
	function save(state) {
		try { localStorage.setItem(KEY, JSON.stringify(state)); } catch (e) {}
	}
	function apply(state) {
		document.documentElement.classList.toggle("show-internal", !!state.showInternal);
	}

	var state = load();
	apply(state);

	document.addEventListener("DOMContentLoaded", function() {
		var btn = document.querySelector(".settings-btn");
		var dlg = document.getElementById("settings-dialog");
		var chk = document.getElementById("settings-show-internal");
		if (!btn || !dlg || !chk) return;
		chk.checked = !!state.showInternal;
		btn.addEventListener("click", function() {
			if (typeof dlg.showModal === "function") dlg.showModal();
			else dlg.setAttribute("open", "open");
		});
		chk.addEventListener("change", function() {
			state.showInternal = chk.checked;
			save(state);
			apply(state);
		});
	});
})();
