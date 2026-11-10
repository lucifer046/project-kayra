// Kayra Bridge — runs inside Google Meet's own page context (world: MAIN).
//
// When Kayra is asked to "share my entire screen", it sets window.__kayraShareSurface =
// "monitor" (or "window" / "browser") and clicks Meet's share button. Meet then calls getDisplayMedia(); this wrapper
// adds Chrome's standard hint so the browser's picker opens on "Entire screen" instead of
// "Chrome tab". It only adds hints the page could have set itself — it never shares
// anything without the picker, and it does nothing unless Kayra asked.
(() => {
  const media = navigator.mediaDevices;
  if (!media || !media.getDisplayMedia || media.__kayraWrapped) return;
  const original = media.getDisplayMedia.bind(media);
  media.getDisplayMedia = function (constraints) {
    const surface = window.__kayraShareSurface;
    if (surface) {
      constraints = Object.assign({}, constraints || {});
      const video = typeof constraints.video === "object" ? Object.assign({}, constraints.video) : {};
      video.displaySurface = surface;            // "monitor" | "window" | "browser"
      constraints.video = video;
      constraints.monitorTypeSurfaces = "include";
      constraints.selfBrowserSurface = "exclude";
      window.__kayraShareSurface = null;          // one share only
    }
    if (window.__kayraShareAudio) {               // "share my screen with sound" (1.4.0)
      constraints = Object.assign({}, constraints || {});
      if (!constraints.audio) constraints.audio = true;
      constraints.systemAudio = "include";
      window.__kayraShareAudio = null;
    }
    return original(constraints);
  };
  media.__kayraWrapped = true;
})();
