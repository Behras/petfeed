self.addEventListener('push', function(event) {
  const data = event.data ? event.data.text() : 'No payload';
  event.waitUntil(
    self.registration.showNotification('🐾 Pet Feeder', {
      body: data
    })
  );
});

