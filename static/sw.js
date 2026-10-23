// TimGym service worker: notifications only (no caching, so pages are always fresh).
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", event => event.waitUntil(self.clients.claim()));

self.addEventListener("push", event => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch (error) { data = {body: event.data && event.data.text()}; }
  event.waitUntil(self.registration.showNotification(data.title || "TimGym", {
    body: data.body || "",
    tag: data.tag || undefined,
    renotify: Boolean(data.tag),
    icon: "/static/icons/icon-192.png",
    badge: "/static/icons/icon-192.png",
    data: {url: data.url || "/"},
  }));
});

self.addEventListener("notificationclick", event => {
  event.notification.close();
  const url = new URL(event.notification.data && event.notification.data.url || "/", self.location.origin).href;
  event.waitUntil((async () => {
    const windows = await self.clients.matchAll({type: "window", includeUncontrolled: true});
    for (const client of windows) {
      if ("focus" in client) {
        await client.focus();
        if ("navigate" in client) return client.navigate(url);
        return;
      }
    }
    return self.clients.openWindow(url);
  })());
});
