// Class-page sidebar resize: drag the handle between the TOC and content to
// adjust sidebar width. Persisted in localStorage so the preference sticks.
(function() {
	var KEY = "bansheedocgenerator.class-sidebar-width";
	var MIN = 220;
	var MAX = 900;
	var layout = document.querySelector(".class-layout");
	var handle = document.getElementById("class-resize-handle");
	var sidebar = document.getElementById("class-sidebar");
	if (!layout || !handle || !sidebar) return;

	var saved = parseInt(localStorage.getItem(KEY) || "", 10);
	if (!isNaN(saved) && saved >= MIN && saved <= MAX) {
		layout.style.setProperty("--class-sidebar-width", saved + "px");
	}

	var dragging = false;
	var startX = 0;
	var startWidth = 0;

	handle.addEventListener("mousedown", function(e) {
		dragging = true;
		startX = e.clientX;
		startWidth = sidebar.getBoundingClientRect().width;
		handle.classList.add("dragging");
		document.body.classList.add("resizing-sidebar");
		e.preventDefault();
	});

	document.addEventListener("mousemove", function(e) {
		if (!dragging) return;
		var delta = e.clientX - startX;
		var next = Math.max(MIN, Math.min(MAX, Math.round(startWidth + delta)));
		layout.style.setProperty("--class-sidebar-width", next + "px");
	});

	document.addEventListener("mouseup", function() {
		if (!dragging) return;
		dragging = false;
		handle.classList.remove("dragging");
		document.body.classList.remove("resizing-sidebar");
		var cur = sidebar.getBoundingClientRect().width;
		localStorage.setItem(KEY, String(Math.round(cur)));
	});
})();
