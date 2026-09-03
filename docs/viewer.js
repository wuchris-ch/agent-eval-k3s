(() => {
  "use strict";

  const stage = document.querySelector("#diagram-stage");
  const canvas = document.querySelector("#diagram-canvas");
  const image = document.querySelector("#diagram-image");
  const title = document.querySelector("#diagram-title");
  const description = document.querySelector("#diagram-description");
  const zoomLevel = document.querySelector("#zoom-level");
  const tabs = [...document.querySelectorAll(".diagram-tab")];
  const pointers = new Map();

  const state = {
    scale: 1,
    x: 0,
    y: 0,
    fitScale: 1,
    imageWidth: 1200,
    imageHeight: 800,
    dragStart: null,
    pinchStart: null,
  };

  const clamp = (value, minimum, maximum) => Math.min(maximum, Math.max(minimum, value));

  function render() {
    canvas.style.transform = `translate3d(${state.x}px, ${state.y}px, 0) scale(${state.scale})`;
    zoomLevel.value = `${Math.round(state.scale * 100)}%`;
    zoomLevel.textContent = zoomLevel.value;
  }

  function fitDiagram() {
    const bounds = stage.getBoundingClientRect();
    const padding = bounds.width < 680 ? 24 : 54;
    const availableWidth = Math.max(1, bounds.width - padding * 2);
    const availableHeight = Math.max(1, bounds.height - padding * 2);

    state.fitScale = Math.min(
      availableWidth / state.imageWidth,
      availableHeight / state.imageHeight,
      1,
    );
    state.scale = state.fitScale;
    state.x = (bounds.width - state.imageWidth * state.scale) / 2;
    state.y = (bounds.height - state.imageHeight * state.scale) / 2;
    render();
  }

  function zoomAt(clientX, clientY, nextScale) {
    const bounds = stage.getBoundingClientRect();
    const pointX = clientX - bounds.left;
    const pointY = clientY - bounds.top;
    const worldX = (pointX - state.x) / state.scale;
    const worldY = (pointY - state.y) / state.scale;
    const minimumScale = Math.min(0.15, state.fitScale * 0.5);

    state.scale = clamp(nextScale, minimumScale, 5);
    state.x = pointX - worldX * state.scale;
    state.y = pointY - worldY * state.scale;
    render();
  }

  function zoomFromCenter(factor) {
    const bounds = stage.getBoundingClientRect();
    zoomAt(
      bounds.left + bounds.width / 2,
      bounds.top + bounds.height / 2,
      state.scale * factor,
    );
  }

  function selectDiagram(tab, updateLocation = true) {
    if (!tab) return;

    for (const candidate of tabs) {
      const active = candidate === tab;
      candidate.classList.toggle("is-active", active);
      candidate.setAttribute("aria-current", active ? "page" : "false");
    }

    title.textContent = tab.dataset.title;
    description.textContent = tab.dataset.description;
    image.alt = `${tab.dataset.title} architecture diagram`;
    state.imageWidth = Number(tab.dataset.width) || image.naturalWidth || 1200;
    state.imageHeight = Number(tab.dataset.height) || image.naturalHeight || 800;
    image.width = state.imageWidth;
    image.height = state.imageHeight;

    if (updateLocation) {
      const url = new URL(window.location.href);
      url.searchParams.set("diagram", tab.dataset.diagram);
      window.history.replaceState({}, "", url);
    }

    if (image.getAttribute("src") === tab.dataset.src && image.complete) {
      fitDiagram();
    } else {
      image.src = tab.dataset.src;
    }
  }

  image.addEventListener("load", () => {
    const activeTab = tabs.find((tab) => tab.classList.contains("is-active"));
    state.imageWidth = Number(activeTab?.dataset.width) || image.naturalWidth || 1200;
    state.imageHeight = Number(activeTab?.dataset.height) || image.naturalHeight || 800;
    image.width = state.imageWidth;
    image.height = state.imageHeight;
    fitDiagram();
  });

  image.addEventListener("error", () => {
    description.textContent = "This diagram could not be loaded. Open its source file from the repository instead.";
  });

  for (const tab of tabs) {
    tab.addEventListener("click", () => selectDiagram(tab));
  }

  stage.addEventListener("wheel", (event) => {
    event.preventDefault();
    const factor = Math.exp(-event.deltaY * 0.0015);
    zoomAt(event.clientX, event.clientY, state.scale * factor);
  }, { passive: false });

  stage.addEventListener("dblclick", (event) => {
    zoomAt(event.clientX, event.clientY, state.scale * 1.6);
  });

  stage.addEventListener("pointerdown", (event) => {
    if (event.button !== 0 || event.target.closest(".viewer-controls")) return;

    stage.setPointerCapture(event.pointerId);
    pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });

    if (pointers.size === 1) {
      state.dragStart = {
        pointerX: event.clientX,
        pointerY: event.clientY,
        x: state.x,
        y: state.y,
      };
      stage.classList.add("is-dragging");
    } else if (pointers.size === 2) {
      const [first, second] = [...pointers.values()];
      const bounds = stage.getBoundingClientRect();
      const midpoint = {
        x: (first.x + second.x) / 2 - bounds.left,
        y: (first.y + second.y) / 2 - bounds.top,
      };
      state.pinchStart = {
        distance: Math.hypot(second.x - first.x, second.y - first.y),
        scale: state.scale,
        worldX: (midpoint.x - state.x) / state.scale,
        worldY: (midpoint.y - state.y) / state.scale,
      };
      state.dragStart = null;
    }
  });

  stage.addEventListener("pointermove", (event) => {
    if (!pointers.has(event.pointerId)) return;
    pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });

    if (pointers.size === 2 && state.pinchStart) {
      const [first, second] = [...pointers.values()];
      const bounds = stage.getBoundingClientRect();
      const midpoint = {
        x: (first.x + second.x) / 2 - bounds.left,
        y: (first.y + second.y) / 2 - bounds.top,
      };
      const distance = Math.hypot(second.x - first.x, second.y - first.y);
      const minimumScale = Math.min(0.15, state.fitScale * 0.5);

      state.scale = clamp(
        state.pinchStart.scale * distance / Math.max(1, state.pinchStart.distance),
        minimumScale,
        5,
      );
      state.x = midpoint.x - state.pinchStart.worldX * state.scale;
      state.y = midpoint.y - state.pinchStart.worldY * state.scale;
      render();
    } else if (pointers.size === 1 && state.dragStart) {
      state.x = state.dragStart.x + event.clientX - state.dragStart.pointerX;
      state.y = state.dragStart.y + event.clientY - state.dragStart.pointerY;
      render();
    }
  });

  function releasePointer(event) {
    pointers.delete(event.pointerId);
    state.pinchStart = null;

    if (pointers.size === 1) {
      const remaining = [...pointers.values()][0];
      state.dragStart = {
        pointerX: remaining.x,
        pointerY: remaining.y,
        x: state.x,
        y: state.y,
      };
    } else {
      state.dragStart = null;
      stage.classList.remove("is-dragging");
    }
  }

  stage.addEventListener("pointerup", releasePointer);
  stage.addEventListener("pointercancel", releasePointer);

  document.querySelector('[data-action="zoom-in"]').addEventListener("click", () => zoomFromCenter(1.25));
  document.querySelector('[data-action="zoom-out"]').addEventListener("click", () => zoomFromCenter(0.8));
  document.querySelector('[data-action="fit"]').addEventListener("click", fitDiagram);
  document.querySelector('[data-action="fullscreen"]').addEventListener("click", async () => {
    if (document.fullscreenElement) {
      await document.exitFullscreen();
    } else if (stage.requestFullscreen) {
      await stage.requestFullscreen();
    }
  });

  document.addEventListener("fullscreenchange", () => {
    const button = document.querySelector('[data-action="fullscreen"]');
    button.textContent = document.fullscreenElement ? "Exit full screen" : "Full screen";
    window.setTimeout(fitDiagram, 50);
  });

  window.addEventListener("keydown", (event) => {
    if (event.key === "+" || event.key === "=") zoomFromCenter(1.25);
    if (event.key === "-") zoomFromCenter(0.8);
    if (event.key === "0") fitDiagram();
  });

  let resizeTimer;
  window.addEventListener("resize", () => {
    window.clearTimeout(resizeTimer);
    resizeTimer = window.setTimeout(fitDiagram, 100);
  });

  const requestedDiagram = new URLSearchParams(window.location.search).get("diagram");
  const requestedTab = tabs.find((tab) => tab.dataset.diagram === requestedDiagram);
  selectDiagram(requestedTab || tabs[0], false);
})();
