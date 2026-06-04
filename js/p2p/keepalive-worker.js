/**
 * 保活 Web Worker
 * 浏览器最小化时 setInterval 会被节流，Web Worker 的定时器不受影响。
 */
self.addEventListener('message', function(e) {
  var msg = e.data;
  if (msg.type === 'start') {
    if (self._timer) self._timer = clearInterval(self._timer);
    self._url = msg.url;
    self._timer = setInterval(function() {
      if (self._url) fetch(self._url).catch(function(){});
    }, 5000);
  } else if (msg.type === 'stop') {
    if (self._timer) { clearInterval(self._timer); self._timer = null; }
    self._url = null;
  }
});
