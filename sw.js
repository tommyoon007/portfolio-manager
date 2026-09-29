const CACHE_NAME = "portfolio-manager-v4";

self.addEventListener("install", () => {
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys
          .filter(
            (key) =>
              key.startsWith("portfolio-manager-") &&
              key !== CACHE_NAME
          )
          .map((key) => caches.delete(key))
      )
    ).then(() => self.clients.claim())
  );
});

// Service Worker는 네트워크 요청을 가로채지 않는다.
// 따라서 GitHub Pages의 최신 index.html을 그대로 사용한다.
self.addEventListener("fetch", () => {
  // 의도적으로 아무것도 하지 않음
});
