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
    this.relayMode = false;       // true = 走服务器中转，false = WebRTC 直连
    this.backendAvail = false;
    this._pollTimer = null;
    this._keepaliveWorker = null;
    this._keepaliveFallbackTimer = null;
    this._fileBuffers = {};
    this._relayFileBuffers = {};  // 中转模式下的文件接收缓冲区
    this._pendingCandidates = {};
    this._pendingAnswers = {};    // peerId -> [answer data] (answer before offer 时排队)

    this.screenStream = null;
    this.userNames = {};

    this.onPeersChange = null;
    this.onMessage = null;
    this.onFile = null;
    this.onScreenStream = null;
    this.onError = null;
    this._pollFailCount = 0;
    this.onFileProgress = null;
    this.onDisconnect = null;
    this.onRelayQuota = null;
    this.onPeerLeave = null;
    this.onScreenEnd = null;
    this.onUserName = null;
    this.onFileMeta = null;        // (peerId, name, size, mime, fileId) => {}
    this._activeTransfers = {};    // transferId -> { cancelled: false }
    this.onTransferStatus = null;  // (id, status) => {}
  }

  /** 安全地将字节数组编码为 base64（避免 String.fromCharCode(...largeArray) 的展开限制） */
  _encodeChunk(chunk) {
    const len = chunk.length;
    let binary = "";
    for (let i = 0; i < len; i++) binary += String.fromCharCode(chunk[i]);
    return btoa(binary);
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
    return this.joinRoomWithConfig(room, 'websrc', '');
  }

  async joinRoomWithConfig(room, roomType, username, password) {
    if (!this.backendAvail) return null;
    try {
      const r = await fetch('/api/p2p/join', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ room, room_type: roomType, username, password: password || '' }),
      });
      if (!r.ok) throw new Error('join failed');
      const data = await r.json();
      if (data.error === 'wrong_password') {
        if (this.onError) this.onError('密码错误');
        return data;
      }
      if (data.error === 'room_full') {
        if (this.onError) this.onError('房间已满');
        return data;
      }
      this.room = room;
      this.peerId = data.peer;
      console.log(`[P2P] Joined "${room}" as ${this.peerId} (type=${roomType}), peers:`, data.peers);
      if (data.peers_info) {
        for (const info of data.peers_info) {
          if (info.name) this.userNames[info.id] = info.name;
        }
      }
      for (const p of data.peers) {
        if (this.relayMode) {
          this._addRelayPeer(p);
        } else {
          this._connectTo(p);
        }
      }
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
    this._startKeepalive();
  }
  _stopPoll() {
    if (this._pollTimer) { clearInterval(this._pollTimer); this._pollTimer = null; }
    this._stopKeepalive();
  }

  async _poll() {
    if (!this.room || !this.peerId) return;
    try {
      const r = await fetch(
        `/api/p2p/signal?room=${encodeURIComponent(this.room)}&peer=${encodeURIComponent(this.peerId)}`
      );
      if (!r.ok) {
        this._pollFail();
        return;
      }
      const data = await r.json();
      this._pollFailCount = 0;  // 成功，重置失败计数
      for (const sig of data.signals) await this._handleSignal(sig);
      if (data.relay && data.relay.length > 0) {
        for (const msg of data.relay) await this._handleRelayData(msg);
      }
      if (this.relayMode && data.relay_remaining !== undefined && this.onRelayQuota) {
        this.onRelayQuota(data.relay_remaining);
      }
    } catch {
      this._pollFail();
    }

    if (!this.relayMode) this._checkPeerConnections();
  }

  /** 轻量保活：每 5s 发送一次，仅更新 last_seen */
  _startKeepalive() {
    try {
      if (!this._keepaliveWorker) {
        this._keepaliveWorker = new Worker("/js/p2p/keepalive-worker.js");
      }
      var url = window.location.origin + "/api/p2p/keepalive?room=" + encodeURIComponent(this.room) + "&peer=" + encodeURIComponent(this.peerId);
      this._keepaliveWorker.postMessage({ type: "start", url: url });
    } catch(e) { /* Web Worker 不支持时降级到 setInterval */
      this._keepaliveFallback();
    }
  }

  _stopKeepalive() {
    if (this._keepaliveWorker) {
      try { this._keepaliveWorker.postMessage({ type: "stop" }); } catch(e) {}
    }
    if (this._keepaliveFallbackTimer) {
      clearInterval(this._keepaliveFallbackTimer);
      this._keepaliveFallbackTimer = null;
    }
  }

  _keepaliveFallback() {
    var self = this;
    this._keepaliveFallbackTimer = setInterval(function() {
      self._keepalive();
    }, 5000);
  }

  async _keepalive() {
    if (!this.room || !this.peerId) return;
    try {
      await fetch(
        `/api/p2p/keepalive?room=${encodeURIComponent(this.room)}&peer=${encodeURIComponent(this.peerId)}`
      );
    } catch { /* ignore */ }
  }

  _pollFail() {
    this._pollFailCount = (this._pollFailCount || 0) + 1;
    if (this._pollFailCount >= 3 && this.onDisconnect) {
      this.onDisconnect('与服务器连接已断开，请检查网络');
    }
  }

  async _handleSignal(sig) {
    switch (sig.type) {
      case 'peer_join':
        if (sig.from !== this.peerId) {
          // 存储用户名（如果提供）
          if (sig.data && sig.data.name) {
            this.userNames[sig.from] = sig.data.name;
          }
          // 中转模式：添加到列表
          if (this.relayMode) {
            this._addRelayPeer(sig.from);
          }
        }
        break;
      case 'peer_leave':
        this._handlePeerLeave(sig.data.peer);
        break;
      case 'offer':
        if (!this.relayMode) await this._handleOffer(sig.from, sig.data);
        break;
      case 'answer':
        if (!this.relayMode) await this._handleAnswer(sig.from, sig.data);
        break;
      case 'ice':
        if (!this.relayMode) await this._handleIce(sig.from, sig.data);
        break;
      case 'screen-offer':
        if (!this.relayMode) await this._handleScreenOffer(sig.from, sig.data);
        break;
      case 'screen-answer':
        if (!this.relayMode) await this._handleScreenAnswer(sig.from, sig.data);
        break;
      case 'screen-end':
        if (!this.relayMode && this.onScreenEnd) this.onScreenEnd(sig.from);
        break;
    }
  }

  // ── 中转模式 ──────────────────────────────────────────

  /** 发送中转数据到另一个 peer */
  sendRelayData(to, type, dataBase64, id) {
    if (!this.room || !this.peerId) return null;
    const msgId = id || Math.random().toString(36).substring(2, 10);
    fetch('/api/p2p/relay/send', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        room: this.room, from: this.peerId, to,
        type, data: dataBase64, id: msgId,
      }),
    }).catch(() => {});
    return msgId;
  }

  /** 处理从中转轮询收到的消息 */
  async _handleRelayData(msg) {
    switch (msg.type) {
      case 'chat':
        if (this.onMessage) {
          try {
            const text = decodeURIComponent(escape(atob(msg.data)));
            this.onMessage(msg.from, text);
          } catch {}
        }
        break;
      case 'file-meta': {
        try {
          const meta = JSON.parse(atob(msg.data));
          this._relayFileBuffers[msg.id] = { ...meta, chunks: [], received: 0 };
          if (this.onFileMeta) this.onFileMeta(msg.from, meta.name, meta.size, meta.mime, msg.id);
        } catch {}
        break;
      }
      case 'file-chunk': {
        const buf = this._relayFileBuffers[msg.id];
        if (!buf) break;
        try {
          const bin = Uint8Array.from(atob(msg.data), c => c.charCodeAt(0));
          buf.chunks.push(bin);
          buf.received += bin.length;
          if (buf.received >= buf.size) {
            const blob = new Blob(buf.chunks, { type: buf.mime });
            if (this.onFile) this.onFile(msg.from, buf.name, buf.mime, blob, msg.id);
            delete this._relayFileBuffers[msg.id];
          }
        } catch {}
        break;
      }
      case 'peer_join':
        // 中转模式：其他 peer 加入时添加到列表
        if (this.relayMode && msg.from !== this.peerId) {
          this._addRelayPeer(msg.from);
        }
        break;
      case 'peer_leave':
        if (this.relayMode) {
          this._handlePeerLeave(msg.from);
        }
        break;
      case 'set-name':
        if (this.relayMode && this.onUserName) {
          try {
            const name = decodeURIComponent(escape(atob(msg.data)));
            this.onUserName(msg.from, name);
          } catch {}
        }
        break;
    }
  }

  _handlePeerLeave(peerId) {
    if (!this.peers[peerId]) return; // 防止广播信号重复处理
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
      if (!this.relayMode) {
        if (p.channel) p.channel.close();
        p.connection.close();
      }
    }
    delete this.peers[peerId];
  }

  /** 中转模式：添加 peer 到列表（不创建 WebRTC 连接） */
  _addRelayPeer(peerId) {
    if (this.peers[peerId] || peerId === this.peerId) return;
    this.peers[peerId] = { connection: null, channel: null, connected: true };
    console.log(`[P2P] Relay peer added: ${peerId}`);
    this._notifyPeersChange();
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
          case 'set-name':
            if (this.onUserName) this.onUserName(peerId, msg.name);
            break;
          case 'file-meta':
            this._fileBuffers[msg.id] = {
              name: msg.name, size: msg.size, mime: msg.mime,
              chunks: [], received: 0,
            };
            if (this.onFileMeta) this.onFileMeta(peerId, msg.name, msg.size, msg.mime, msg.id);
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

  /** 已连接列表：DataChannel open、WebRTC 就绪 或 中转模式 */
  _connectedPeerList() {
    return Object.keys(this.peers).filter((id) => {
      const p = this.peers[id];
      if (this.relayMode) return true;
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
    if (this.relayMode) {
      const encoded = btoa(unescape(encodeURIComponent(text)));
      for (const pid of Object.keys(this.peers)) {
        if (pid !== this.peerId) this.sendRelayData(pid, 'chat', encoded);
      }
      return;
    }
    const msg = JSON.stringify({ type: 'chat', text });
    for (const p of Object.values(this.peers)) {
      if (p.channel && p.channel.readyState === 'open') p.channel.send(msg);
    }
  }

  /** 发送文件。返回 transferId（字符串）表示已开始发送，null 表示失败/被限制。 */
  sendFile(file) {
    const CHUNK = 16 * 1024;
    const FILE_MAX_SIZE = 5 * 1024 * 1024;  // 5MB
    const KB = 1024;
    const SPEED_KBPS = 100;    // 100 KB/s
    const CHUNK_DELAY = Math.ceil(CHUNK / (SPEED_KBPS * KB) * 1000);  // ms between chunks

    if (file.size > FILE_MAX_SIZE) {
      if (this.onError) this.onError('文件超过 5MB 限制，无法发送');
      return null;
    }

    const transferId = Math.random().toString(36).substring(2, 10);
    this._activeTransfers[transferId] = { cancelled: false, file: file };
    if (this.onTransferStatus) this.onTransferStatus(transferId, 'sending');

    if (this.relayMode) {
      const targetPeers = Object.keys(this.peers).filter(p => p !== this.peerId);
      if (!targetPeers.length) {
        delete this._activeTransfers[transferId];
        return null;
      }
      const meta = JSON.stringify({
        name: file.name, size: file.size,
        mime: file.type || 'application/octet-stream',
      });
      for (const pid of targetPeers) this.sendRelayData(pid, 'file-meta', btoa(meta), transferId);
      // 异步分片发送
      this._sendRelayFileChunks(file, transferId, targetPeers, CHUNK, CHUNK_DELAY);
      return transferId;
    }

    // ── WebRTC 模式 ──
    const peers = Object.values(this.peers).filter(
      (p) => p.channel && p.channel.readyState === 'open'
    );
    if (!peers.length) {
      delete this._activeTransfers[transferId];
      return null;
    }
    const meta = JSON.stringify({
      type: 'file-meta', id: transferId, name: file.name,
      size: file.size, mime: file.type || 'application/octet-stream',
    });
    for (const p of peers) p.channel.send(meta);
    const self = this;
    const reader = new FileReader();
    reader.onload = (ev) => {
      if (!ev.target) return;
      const arr = new Uint8Array(ev.target.result);
      const total = Math.ceil(arr.length / CHUNK);
      for (let i = 0; i < total; i++) {
        if (self._activeTransfers[transferId]?.cancelled) {
          delete self._activeTransfers[transferId];
          if (self.onTransferStatus) self.onTransferStatus(transferId, 'cancelled');
          return;
        }
        const chunk = arr.slice(i * CHUNK, (i + 1) * CHUNK);
        const b64 = self._encodeChunk(chunk);
        const msg = JSON.stringify({ type: 'file-chunk', id: transferId, seq: i, total, data: b64 });
        for (const p of peers) p.channel.send(msg);
        if (self.onFileProgress) {
          self.onFileProgress({ sent: Math.min((i + 1) * CHUNK, arr.length), total: arr.length, id: transferId });
        }
      }
      delete self._activeTransfers[transferId];
      if (self.onTransferStatus) self.onTransferStatus(transferId, 'sent');
    };
    reader.onerror = () => {
      delete self._activeTransfers[transferId];
      if (self.onTransferStatus) self.onTransferStatus(transferId, 'failed');
    };
    reader.readAsArrayBuffer(file);
    return transferId;
  }

  /** 中转模式：限速分片发送文件 */
  async _sendRelayFileChunks(file, id, targetPeers, CHUNK, delay) {
    const totalChunks = Math.ceil(file.size / CHUNK);
    let sentBytes = 0;

    // 读文件
    const buffer = await new Promise((resolve, reject) => {
      const r = new FileReader();
      r.onload = e => resolve(e.target.result);
      r.onerror = reject;
      r.readAsArrayBuffer(file);
    });
    const arr = new Uint8Array(buffer);
    const fileSize = arr.length;

    for (let i = 0; i < totalChunks; i++) {
      if (this._activeTransfers[id]?.cancelled) {
        delete this._activeTransfers[id];
        if (this.onTransferStatus) this.onTransferStatus(id, 'cancelled');
        return;
      }
      const chunk = arr.slice(i * CHUNK, (i + 1) * CHUNK);
      const b64 = this._encodeChunk(chunk);
      sentBytes += chunk.length;
      for (const pid of targetPeers) {
        this.sendRelayData(pid, 'file-chunk', b64, id);
      }
      // 报告进度
      if (this.onFileProgress) {
        this.onFileProgress({ sent: sentBytes, total: fileSize, id });
      } else {
        console.log('[P2P] No onFileProgress callback for chunk', i, '/', totalChunks);
      }
      // 限速：等待后再发下一个 chunk
      if (i < totalChunks - 1) {
        await new Promise(r => setTimeout(r, delay));
      }
    }
    // 完成
    if (this.onFileProgress) {
      this.onFileProgress({ sent: fileSize, total: fileSize, id, done: true });
    }
    delete this._activeTransfers[id];
    if (this.onTransferStatus) this.onTransferStatus(id, 'sent');
  }

  /** 中转发送文件的方法名（供上层调用） */
  async startScreenShare() {
    try {
      const stream = await navigator.mediaDevices.getDisplayMedia({ video: true });
      this.screenStream = stream;

      for (const [pid, p] of Object.entries(this.peers)) {
        for (const track of stream.getVideoTracks()) {
          p.connection.addTrack(track, stream);
        }
        const offer = await p.connection.createOffer();
        await p.connection.setLocalDescription(offer);
        this._sendSignal(pid, 'screen-offer', {
          sdp: offer.sdp, type: offer.type,
        });
      }

      stream.getVideoTracks()[0].onended = () => {
        this.stopScreenShare();
      };
      return true;
    } catch { return false; }
  }

  stopScreenShare() {
    if (this.screenStream) {
      this.screenStream.getTracks().forEach(t => t.stop());
      this.screenStream = null;
    }
    // 通知本地 UI
    if (this.onScreenEnd) this.onScreenEnd(this.peerId);
    // 通知远端 peer
    for (const pid of Object.keys(this.peers)) {
      this._sendSignal(pid, 'screen-end', {});
    }
  }

  broadcastUserName(name) {
    const msg = JSON.stringify({ type: 'set-name', name });
    for (const p of Object.values(this.peers)) {
      // WebRTC 模式：通过 DataChannel 发送
      if (p.channel && p.channel.readyState === 'open') p.channel.send(msg);
    }
    // 中转模式：通过服务器广播
    if (this.relayMode) {
      const encoded = btoa(unescape(encodeURIComponent(name)));
      for (const pid of Object.keys(this.peers)) {
        if (pid !== this.peerId) this.sendRelayData(pid, 'set-name', encoded);
      }
    }
  }

  cancelTransfer(transferId) {
    const t = this._activeTransfers[transferId];
    if (t) t.cancelled = true;
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
