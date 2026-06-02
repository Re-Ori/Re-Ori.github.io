/**
 * P2P Manager — WebRTC 点对点通信
 * 支持：文字聊天、文件/图片传输、屏幕共享
 */
class P2PManager {
  constructor() {
    this.room = '';
    this.peerId = '';
    this.peers = {};           // peerId -> { connection, channel }
    this.backendAvail = false;
    this._pollTimer = null;
    this._fileBuffers = {};    // fileId -> received chunks
    this._pendingCandidates = {}; // peerId -> [candidates] (waiting for setRemoteDescription)

    // 回调
    this.onPeersChange = null;   // (peerIds) => {}
    this.onMessage = null;       // (peerId, text) => {}
    this.onFile = null;          // (peerId, name, mime, blob) => {}
    this.onScreenStream = null;  // (peerId, stream) => {}
    this.onError = null;         // (msg) => {}
  }

  // ── 后端检测 ──

  async checkBackend() {
    try {
      const r = await fetch('/api/ping', { cache: 'no-store' });
      this.backendAvail = r.ok;
    } catch { this.backendAvail = false; }
    return this.backendAvail;
  }

  // ── 房间管理 ──

  async joinRoom(room) {
    if (!this.backendAvail) return null;
    try {
      const r = await fetch('/api/p2p/join', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ room }),
      });
      if (!r.ok) throw new Error('join failed');
      const data = await r.json();
      this.room = room;
      this.peerId = data.peer;
      for (const p of data.peers) this._connectTo(p);
      this._startPoll();
      return data;
    } catch (e) {
      if (this.onError) this.onError('加入房间失败: ' + e.message);
      return null;
    }
  }

  async leaveRoom() {
    this._stopPoll();
    for (const id of Object.keys(this.peers)) this._disconnectPeer(id);
    this.peers = {};
    try {
      await fetch('/api/p2p/leave', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ room: this.room, peer: this.peerId }),
      });
    } catch { /* ignore */ }
    this.room = '';
    this.peerId = '';
  }

  // ── 信令轮询 ──

  _startPoll() {
    this._pollTimer = setInterval(() => this._poll(), 1200);
  }
  _stopPoll() {
    if (this._pollTimer) { clearInterval(this._pollTimer); this._pollTimer = null; }
  }

  async _poll() {
    if (!this.room || !this.peerId) return;
    try {
      const r = await fetch(`/api/p2p/signal?room=${encodeURIComponent(this.room)}&peer=${encodeURIComponent(this.peerId)}`);
      if (!r.ok) return;
      const data = await r.json();
      for (const sig of data.signals) await this._handleSignal(sig);
    } catch { /* ignore polling errors */ }
  }

  async _handleSignal(sig) {
    switch (sig.type) {
      case 'peer_join':
        this._connectTo(sig.data.peer);
        break;
      case 'offer':
        await this._handleOffer(sig.from, sig.data);
        break;
      case 'answer':
        await this._handleAnswer(sig.from, sig.data);
        break;
      case 'ice':
        await this._handleIce(sig.from, sig.data);
        break;
      case 'screen-offer':
        await this._handleScreenOffer(sig.from, sig.data);
        break;
      case 'screen-answer':
        await this._handleScreenAnswer(sig.from, sig.data);
        break;
    }
  }

  // ── WebRTC 连接管理 ──

  _getOrCreatePC(peerId) {
    if (this.peers[peerId]) return this.peers[peerId].connection;
    const pc = new RTCPeerConnection({ iceServers: [{ urls: 'stun:stun.l.google.com:19302' }] });
    this.peers[peerId] = { connection: pc, channel: null };

    pc.onicecandidate = (e) => {
      if (e.candidate) {
        this._sendSignal(peerId, 'ice', e.candidate.toJSON());
      }
    };
    pc.ondatachannel = (e) => this._setupChannel(e.channel, peerId);
    pc.ontrack = (e) => {
      if (this.onScreenStream) this.onScreenStream(peerId, e.streams[0]);
    };

    // Flush any pending candidates
    const pending = this._pendingCandidates[peerId] || [];
    delete this._pendingCandidates[peerId];
    for (const c of pending) pc.addIceCandidate(new RTCIceCandidate(c)).catch(() => {});

    return pc;
  }

  // ── 连接修复：Polite Peer 模式 ──
  // 双方都创建 DataChannel + Offer。
  // 当收到对方的 Offer 时根据自己的 ID 决定 polite/imolite 行为，
  // 彻底解决 glare（信号冲突）问题。

  _connectTo(peerId) {
    if (this.peers[peerId] || peerId === this.peerId) return;
    const pc = this._getOrCreatePC(peerId);
    const channel = pc.createDataChannel('p2p-channel');
    this._setupChannel(channel, peerId);

    pc.createOffer().then(offer => {
      // createOffer 是异步的，期间可能已经收到对方 Offer 并改变了状态
      if (pc.signalingState !== 'stable' || pc.remoteDescription) return;
      return pc.setLocalDescription(offer);
    }).then(() => {
      if (pc.localDescription && pc.localDescription.type === 'offer') {
        this._sendSignal(peerId, 'offer', {
          sdp: pc.localDescription.sdp,
          type: pc.localDescription.type
        });
      }
    }).catch(e => console.error('createOffer error', e));
  }

  _disconnectPeer(peerId) {
    const p = this.peers[peerId];
    if (p) {
      if (p.channel) p.channel.close();
      p.connection.close();
    }
    delete this.peers[peerId];
  }

  async _handleOffer(from, data) {
    const pc = this._getOrCreatePC(from);

    // Polite Peer 模式：双方同时发 Offer（glare）时的处理
    if (pc.signalingState === 'have-local-offer') {
      if (this.peerId > from) {
        // ID 较大 = polite：回滚已有 offer，接受对方
        await pc.setLocalDescription({ type: 'rollback' });
      } else {
        // ID 较小 = impolite：保留自己 offer，忽略对方
        return;
      }
    }

    try {
      await pc.setRemoteDescription(new RTCSessionDescription(data));
      const answer = await pc.createAnswer();
      await pc.setLocalDescription(answer);
      this._sendSignal(from, 'answer', { sdp: answer.sdp, type: answer.type });
    } catch (e) { console.error('handleOffer error', from, e); }
  }

  async _handleAnswer(from, data) {
    const p = this.peers[from];
    if (!p) return;
    if (p.connection.signalingState === 'stable') return;
    try {
      await p.connection.setRemoteDescription(new RTCSessionDescription(data));
    } catch (e) { console.error('handleAnswer error', from, e); }
  }

  async _handleIce(from, data) {
    const p = this.peers[from];
    if (!p) {
      (this._pendingCandidates[from] = this._pendingCandidates[from] || []).push(data);
      return;
    }
    try {
      await p.connection.addIceCandidate(new RTCIceCandidate(data));
    } catch { /* ignore */ }
  }

  _setupChannel(channel, peerId) {
    const p = this.peers[peerId];
    if (!p) return;
    p.channel = channel;

    channel.onopen = () => {
      if (this.onPeersChange) this.onPeersChange(this._peerList());
    };
    channel.onclose = () => {
      delete this.peers[peerId];
      if (this.onPeersChange) this.onPeersChange(this._peerList());
    };

    channel.onmessage = (e) => {
      try {
        const msg = JSON.parse(e.data);
        switch (msg.type) {
          case 'chat':
            if (this.onMessage) this.onMessage(peerId, msg.text);
            break;
          case 'ping':
            channel.send(JSON.stringify({ type: 'pong' }));
            break;
          case 'file-meta':
            this._fileBuffers[msg.id] = { name: msg.name, size: msg.size, mime: msg.mime, chunks: [], received: 0 };
            break;
          case 'file-chunk': {
            const buf = this._fileBuffers[msg.id];
            if (!buf) break;
            const bin = Uint8Array.from(atob(msg.data), c => c.charCodeAt(0));
            buf.chunks.push(bin);
            buf.received += bin.length;
            if (buf.received >= buf.size) {
              const blob = new Blob(buf.chunks, { type: buf.mime });
              if (this.onFile) this.onFile(peerId, buf.name, buf.mime, blob, msg.id);
              delete this._fileBuffers[msg.id];
            }
            break;
          }
        }
      } catch { /* ignore parse errors */ }
    };
  }

  _peerList() {
    return Object.keys(this.peers).filter(id => {
      const p = this.peers[id];
      return p.channel && p.channel.readyState === 'open';
    });
  }

  _sendSignal(to, type, data) {
    fetch('/api/p2p/signal', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ room: this.room, from: this.peerId, to, type, data }),
    }).catch(() => {});
  }

  // ── 对外接口 ──

  sendChat(text) {
    const msg = JSON.stringify({ type: 'chat', text });
    for (const p of Object.values(this.peers)) {
      if (p.channel && p.channel.readyState === 'open') p.channel.send(msg);
    }
  }

  sendFile(file) {
    const id = Math.random().toString(36).substring(2, 10);
    const CHUNK = 16 * 1024; // 16KB

    const peers = Object.values(this.peers).filter(p => p.channel && p.channel.readyState === 'open');
    if (!peers.length) return;

    // Send meta first
    const meta = JSON.stringify({ type: 'file-meta', id, name: file.name, size: file.size, mime: file.type || 'application/octet-stream' });
    for (const p of peers) p.channel.send(meta);

    // Read and chunk
    const reader = new FileReader();
    reader.onload = (ev) => {
      const arr = new Uint8Array(ev.target.result);
      const total = Math.ceil(arr.length / CHUNK);
      for (let i = 0; i < total; i++) {
        const chunk = arr.slice(i * CHUNK, (i + 1) * CHUNK);
        const b64 = btoa(String.fromCharCode(...chunk));
        const msg = JSON.stringify({ type: 'file-chunk', id, seq: i, total, data: b64 });
        for (const p of peers) p.channel.send(msg);
      }
    };
    reader.readAsArrayBuffer(file);
  }

  async startScreenShare() {
    try {
      const stream = await navigator.mediaDevices.getDisplayMedia({ video: true });
      for (const p of Object.values(this.peers)) {
        for (const track of stream.getVideoTracks()) {
          p.connection.addTrack(track, stream);
        }
        const offer = await p.connection.createOffer();
        await p.connection.setLocalDescription(offer);
        // Find peerId for this connection
        let pid = null;
        for (const [id, v] of Object.entries(this.peers)) {
          if (v.connection === p.connection) { pid = id; break; }
        }
        if (pid) this._sendSignal(pid, 'screen-offer', { sdp: offer.sdp, type: offer.type });
      }
      stream.getVideoTracks()[0].onended = () => {};
      return true;
    } catch { return false; }
  }

  async _handleScreenOffer(from, data) {
    const p = this.peers[from];
    if (!p) return;
    try {
      await p.connection.setRemoteDescription(new RTCSessionDescription(data));
      const answer = await p.connection.createAnswer();
      await p.connection.setLocalDescription(answer);
      this._sendSignal(from, 'screen-answer', { sdp: answer.sdp, type: answer.type });
    } catch (e) { console.error('screen-offer error', e); }
  }

  async _handleScreenAnswer(from, data) {
    const p = this.peers[from];
    if (!p) return;
    try {
      await p.connection.setRemoteDescription(new RTCSessionDescription(data));
    } catch (e) { console.error('screen-answer error', e); }
  }
}
