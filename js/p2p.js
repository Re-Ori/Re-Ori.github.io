/**
 * P2P Manager — WebRTC 点对点通信
 *
 * 单方发起：只有加入房间的 peer 主动创建 offer，
 * 已有 peer 只响应收到的 offer，完全消除 glare。
 *
 * 健壮性策略：
 * - 每次 poll 检查连接状态，`connected` flag 跟踪连接就绪
 * - `_connectedPeerList()` 检查 DataChannel + connected flag
 * - 移除 auto-cleanup 以免初始连接阶段误删 peer
 * - 完整日志输出到浏览器控制台以便调试
 */
class P2PManager {
  constructor() {
    this.room = '';
    this.peerId = '';
    this.peers = {};              // peerId -> { connection, channel, connected }
    this.backendAvail = false;
    this._pollTimer = null;
    this._fileBuffers = {};
    this._pendingCandidates = {};
    this._pendingAnswers = {};    // peerId -> [answer data] (answer before offer 时排队)

    this.onPeersChange = null;
    this.onMessage = null;
    this.onFile = null;
    this.onScreenStream = null;
    this.onError = null;
    this.onPeerLeave = null;
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
      console.log(`[P2P] Joined "${room}" as ${this.peerId}, peers:`, data.peers);
      for (const p of data.peers) this._connectTo(p);
      this._notifyPeersChange();
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
    console.log(`[P2P] Left "${this.room}"`);
    this.room = '';
    this.peerId = '';
  }

  // ── 信令轮询 ──

  _startPoll() {
    this._stopPoll();
    this._pollTimer = setInterval(() => this._poll(), 1200);
  }
  _stopPoll() {
    if (this._pollTimer) { clearInterval(this._pollTimer); this._pollTimer = null; }
  }

  async _poll() {
    if (!this.room || !this.peerId) return;
    try {
      const r = await fetch(
        `/api/p2p/signal?room=${encodeURIComponent(this.room)}&peer=${encodeURIComponent(this.peerId)}`
      );
      if (!r.ok) return;
      const data = await r.json();
      for (const sig of data.signals) await this._handleSignal(sig);
    } catch { /* ignore polling errors */ }

    // 每次 poll 后检查连接状态，确保 UI 及时更新
    this._checkPeerConnections();
  }

  async _handleSignal(sig) {
    switch (sig.type) {
      case 'peer_join':
        break;
      case 'peer_leave':
        this._handlePeerLeave(sig.data.peer);
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

  _handlePeerLeave(peerId) {
    console.log(`[P2P] Peer left: ${peerId}`);
    this._disconnectPeer(peerId);
    this._notifyPeersChange();
    if (this.onPeerLeave) this.onPeerLeave(peerId);
  }

  // ── WebRTC 连接管理 ──

  _getOrCreatePC(peerId) {
    if (this.peers[peerId]) return this.peers[peerId].connection;
    const pc = new RTCPeerConnection({
      iceServers: [{ urls: 'stun:stun.l.google.com:19302' }]
    });
    this.peers[peerId] = { connection: pc, channel: null, connected: false, connectStartedAt: 0 };

    pc.onicecandidate = (e) => {
      if (e.candidate) {
        this._sendSignal(peerId, 'ice', e.candidate.toJSON());
      }
    };
    pc.ondatachannel = (e) => {
      this._setupChannel(e.channel, peerId);
    };
    pc.ontrack = (e) => {
      if (this.onScreenStream) this.onScreenStream(peerId, e.streams[0]);
    };

    pc.oniceconnectionstatechange = () => {
      const s = pc.iceConnectionState;
      console.log(`[P2P] ICE ${peerId}: ${s}`);
      if (s === 'connected' || s === 'completed') {
        // ICE 连接就绪 → 标记连接已建立
        const p = this.peers[peerId];
        if (p && !p.connected) {
          p.connected = true;
          this._notifyPeersChange();
        }
      }
    };
    pc.onconnectionstatechange = () => {
      const s = pc.connectionState;
      console.log(`[P2P] Conn ${peerId}: ${s}`);
      if (s === 'connected') {
        const p = this.peers[peerId];
        if (p && !p.connected) {
          p.connected = true;
          this._notifyPeersChange();
        }
      }
    };

    return pc;
  }

  /** 检查所有 peer 的连接状态，并处理超时重试 */
  _checkPeerConnections() {
    let changed = false;
    const now = Date.now();

    for (const [id, p] of Object.entries(this.peers)) {
      const conn = p.connection;
      const iceState = conn.iceConnectionState;
      const connState = conn.connectionState;
      const channelOpen = p.channel && p.channel.readyState === 'open';

      // 标记已连接
      if ((connState === 'connected' || iceState === 'connected' || iceState === 'completed') && !p.connected) {
        console.log(`[P2P] ${id} connected (conn=${connState}, ice=${iceState}, chan=${channelOpen ? 'open' : 'waiting'})`);
        p.connected = true;
        changed = true;
      }

      // 超时重试：10s 未连接且是我们发起的
      if (!p.connected && p.connectStartedAt > 0 && (now - p.connectStartedAt) > 10000) {
        const iceFailed = iceState === 'failed';
        const connFailed = connState === 'failed' || connState === 'disconnected';
        console.log(`[P2P] ${id} timeout (${Math.round((now - p.connectStartedAt)/1000)}s, ice=${iceState}, conn=${connState}), reconnecting...`);
        // 断开重连
        this._disconnectPeer(id);
        this._connectTo(id);
        // _connectTo 会更新 connectStartedAt
      }
    }

    if (changed) this._notifyPeersChange();
  }

  _flushPendingCandidates(peerId, pc) {
    const pending = this._pendingCandidates[peerId] || [];
    delete this._pendingCandidates[peerId];
    for (const c of pending) {
      try { pc.addIceCandidate(new RTCIceCandidate(c)); } catch {}
    }
  }

  _connectTo(peerId) {
    if (this.peers[peerId] || peerId === this.peerId) return;
    const pc = this._getOrCreatePC(peerId);
    this.peers[peerId].connectStartedAt = Date.now();
    console.log(`[P2P] Connecting to ${peerId}...`);

    // 使用 negotiated DataChannel：双方各自创建相同 ID 的通道，
    // 不依赖 ondatachannel 事件，更可靠
    const channel = pc.createDataChannel('p2p-channel', { negotiated: true, id: 0 });
    this._setupChannel(channel, peerId);

    pc.createOffer()
      .then((offer) => {
        if (pc.signalingState !== 'stable') return;
        return pc.setLocalDescription(offer);
      })
      .then(() => {
        if (pc.localDescription && pc.signalingState === 'have-local-offer') {
          console.log(`[P2P] Sending offer to ${peerId}`);
          this._sendSignal(peerId, 'offer', {
            sdp: pc.localDescription.sdp,
            type: pc.localDescription.type,
          });

          // 处理在 offer 创建前到达的排队 answer
          const pending = (this._pendingAnswers[peerId] || []);
          delete this._pendingAnswers[peerId];
          for (const ans of pending) {
            console.log(`[P2P] Processing queued answer from ${peerId}`);
            this._handleAnswer(peerId, ans);
          }
        }
      })
      .catch((e) => console.error('[P2P] createOffer error', e));
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
    console.log(`[P2P] Received offer from ${from}`);
    const pc = this._getOrCreatePC(from);

    try {
      await pc.setRemoteDescription(new RTCSessionDescription(data));
      console.log(`[P2P] Remote description set for ${from}`);
      this._flushPendingCandidates(from, pc);

      // 创建同样 negotiated DataChannel（双方 ID 必须一致）
      // 不依赖 ondatachannel，避免浏览器兼容问题
      if (!this.peers[from].channel) {
        console.log(`[P2P] Creating negotiated DataChannel for ${from}`);
        const channel = pc.createDataChannel('p2p-channel', { negotiated: true, id: 0 });
        this._setupChannel(channel, from);
      }

      const answer = await pc.createAnswer();
      await pc.setLocalDescription(answer);
      console.log(`[P2P] Sending answer to ${from}`);
      this._sendSignal(from, 'answer', {
        sdp: answer.sdp,
        type: answer.type,
      });
    } catch (e) {
      console.error('[P2P] handleOffer error', from, e);
    }
  }

  async _handleAnswer(from, data) {
    const p = this.peers[from];
    if (!p) { console.warn(`[P2P] Answer from unknown ${from}`); return; }

    console.log(`[P2P] Answer from ${from}, signalingState=${p.connection.signalingState}, hasLocalDesc=${!!p.connection.localDescription}`);

    // 如果还没设 local description（offer 尚未 create），排队等
    if (p.connection.signalingState === 'stable' && !p.connection.localDescription) {
      console.log(`[P2P] Answer from ${from} queued (offer not yet set)`);
      (this._pendingAnswers[from] = this._pendingAnswers[from] || []).push(data);
      return;
    }

    // 已处理过相同 answer
    if (p.connection.signalingState === 'stable') return;

    try {
      await p.connection.setRemoteDescription(new RTCSessionDescription(data));
      console.log(`[P2P] Remote description set from ${from} via answer`);
      this._flushPendingCandidates(from, p.connection);
    } catch (e) {
      console.error('[P2P] handleAnswer error', from, e);
    }
  }

  async _handleIce(from, data) {
    const p = this.peers[from];
    if (!p || !p.connection.remoteDescription) {
      (this._pendingCandidates[from] = this._pendingCandidates[from] || []).push(data);
      return;
    }
    try {
      await p.connection.addIceCandidate(new RTCIceCandidate(data));
    } catch { /* ignore */ }
  }

  _setupChannel(channel, peerId) {
    const p = this.peers[peerId];
    if (!p) { console.warn(`[P2P] setupChannel: peer ${peerId} not found`); return; }
    p.channel = channel;

    channel.onopen = () => {
      console.log(`[P2P] DataChannel opened for ${peerId}`);
      p.connected = true;
      this._notifyPeersChange();
    };

    channel.onclose = () => {
      console.log(`[P2P] DataChannel closed for ${peerId}`);
      if (this.peers[peerId] && this.peers[peerId].channel === channel) {
        delete this.peers[peerId];
        this._notifyPeersChange();
      }
    };

    channel.onerror = (e) => {
      console.error(`[P2P] DataChannel error for ${peerId}:`, e);
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
            this._fileBuffers[msg.id] = {
              name: msg.name, size: msg.size, mime: msg.mime,
              chunks: [], received: 0,
            };
            break;
          case 'file-chunk': {
            const buf = this._fileBuffers[msg.id];
            if (!buf) break;
            const bin = Uint8Array.from(atob(msg.data), (c) => c.charCodeAt(0));
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

  /** 已连接列表：DataChannel open 或 WebRTC 连接就绪 */
  _connectedPeerList() {
    return Object.keys(this.peers).filter((id) => {
      const p = this.peers[id];
      if (p.channel && p.channel.readyState === 'open') return true;
      if (p.connected) return true;
      return false;
    });
  }

  _allPeerList() {
    return Object.keys(this.peers);
  }

  _notifyPeersChange() {
    if (this.onPeersChange) {
      this.onPeersChange(this._connectedPeerList(), this._allPeerList());
    }
  }

  _sendSignal(to, type, data) {
    if (!this.room || !this.peerId) return;
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
    const CHUNK = 16 * 1024;
    const peers = Object.values(this.peers).filter(
      (p) => p.channel && p.channel.readyState === 'open'
    );
    if (!peers.length) return;
    const meta = JSON.stringify({
      type: 'file-meta', id, name: file.name,
      size: file.size, mime: file.type || 'application/octet-stream',
    });
    for (const p of peers) p.channel.send(meta);
    const reader = new FileReader();
    reader.onload = (ev) => {
      if (!ev.target) return;
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
        let pid = null;
        for (const [id, v] of Object.entries(this.peers)) {
          if (v.connection === p.connection) { pid = id; break; }
        }
        if (pid) {
          this._sendSignal(pid, 'screen-offer', {
            sdp: offer.sdp, type: offer.type,
          });
        }
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
    } catch (e) { console.error('[P2P] screen-offer error', e); }
  }

  async _handleScreenAnswer(from, data) {
    const p = this.peers[from];
    if (!p) return;
    try {
      await p.connection.setRemoteDescription(new RTCSessionDescription(data));
    } catch (e) { console.error('[P2P] screen-answer error', e); }
  }
}
