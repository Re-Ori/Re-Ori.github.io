/**
 * P2P Manager — WebRTC 点对点通信
 * 支持：文字聊天、文件/图片传输、屏幕共享
 *
 * 修复记录（v2）：
 * - 修复 channel.onclose 中 orphaned 通道误删 peer 的问题：
 *   只有当关闭的 channel 仍是当前活跃的 channel 时才执行删除
 * - 修复 ICE 候选时序问题：在 setRemoteDescription 之前到达的候选
 *   被正确排队，在 setRemoteDescription 完成后刷新
 * - 加入日志便于调试
 */
class P2PManager {
  constructor() {
    this.room = '';
    this.peerId = '';
    this.peers = {};           // peerId -> { connection, channel }
    this.backendAvail = false;
    this._pollTimer = null;
    this._fileBuffers = {};    // fileId -> received chunks
    this._pendingCandidates = {}; // peerId -> [candidates]

    // 回调
    this.onPeersChange = null;   // (connectedIds, allIds) => {}
    this.onMessage = null;       // (peerId, text) => {}
    this.onFile = null;          // (peerId, name, mime, blob, fileId) => {}
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
      // 连接房间内已有成员
      for (const p of data.peers) this._connectTo(p);
      // 立即触发一次上报，方便 UI 显示初始状态
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
    const pc = new RTCPeerConnection({
      iceServers: [{ urls: 'stun:stun.l.google.com:19302' }]
    });
    this.peers[peerId] = { connection: pc, channel: null };

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

    return pc;
  }

  /** 刷新指定 peer 的排队 ICE 候选（在 setRemoteDescription 后调用） */
  _flushPendingCandidates(peerId, pc) {
    const pending = this._pendingCandidates[peerId] || [];
    delete this._pendingCandidates[peerId];
    for (const c of pending) {
      try {
        pc.addIceCandidate(new RTCIceCandidate(c));
      } catch { /* ignore */ }
    }
  }

  /**
   * Polite Peer 模式（彻底解决 glare / 信号冲突）：
   *
   * 双方都会创建 DataChannel + Offer，
   * 收到对方 Offer 时通过 ID 比较决定谁让步：
   *   - peerId > from（较大 ID）：polite → 回滚自己的 offer，接受对方
   *   - peerId < from（较小 ID）：impolite → 保留自己的 offer，忽略对方
   *
   * 保证在相互发起的情况下总有一方能完成连接，不会死锁。
   */
  _connectTo(peerId) {
    if (this.peers[peerId] || peerId === this.peerId) return;
    const pc = this._getOrCreatePC(peerId);
    const channel = pc.createDataChannel('p2p-channel');
    this._setupChannel(channel, peerId);

    pc.createOffer()
      .then((offer) => {
        // 防止双重协商：如果在 createOffer 异步期间
        // _handleOffer 已经完成了 SDP 交换，就不再发送新的 offer
        if (pc.signalingState !== 'stable' || pc.currentRemoteDescription) return;
        return pc.setLocalDescription(offer);
      })
      .then(() => {
        if (pc.localDescription && pc.signalingState === 'have-local-offer') {
          this._sendSignal(peerId, 'offer', {
            sdp: pc.localDescription.sdp,
            type: pc.localDescription.type,
          });
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
    const pc = this._getOrCreatePC(from);

    // Glare 处理：双方同时发 offer
    if (pc.signalingState === 'have-local-offer') {
      if (this.peerId > from) {
        // polite：接受对方的 offer
        await pc.setLocalDescription({ type: 'rollback' });
      } else {
        // impolite：保留自己的 offer
        return;
      }
    }

    try {
      await pc.setRemoteDescription(new RTCSessionDescription(data));
      // 关键修复：setRemoteDescription 后刷新排队中的 ICE 候选
      this._flushPendingCandidates(from, pc);

      const answer = await pc.createAnswer();
      await pc.setLocalDescription(answer);
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
    if (!p) return;
    if (p.connection.signalingState === 'stable') return;
    try {
      await p.connection.setRemoteDescription(new RTCSessionDescription(data));
      // 关键修复：setRemoteDescription 后刷新排队中的 ICE 候选
      this._flushPendingCandidates(from, p.connection);
    } catch (e) {
      console.error('[P2P] handleAnswer error', from, e);
    }
  }

  async _handleIce(from, data) {
    const p = this.peers[from];
    // 如果 peer 不存在，或还未设置 remote description，则排队
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
    if (!p) return;
    p.channel = channel;

    channel.onopen = () => {
      this._notifyPeersChange();
    };

    channel.onclose = () => {
      // 关键修复：仅当关闭的 channel 仍然是当前活跃的 channel 时才删除 peer
      // 防止孤儿 channel（rollback 遗留）关闭时误删正在使用的连接
      if (this.peers[peerId] && this.peers[peerId].channel === channel) {
        delete this.peers[peerId];
        this._notifyPeersChange();
      }
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

  /** 获取已连接（DataChannel 已 open）的 peer 列表 */
  _connectedPeerList() {
    return Object.keys(this.peers).filter((id) => {
      const p = this.peers[id];
      return p.channel && p.channel.readyState === 'open';
    });
  }

  /** 获取所有已建立 RTCPeerConnection 的 peer 列表（含正在连接的） */
  _allPeerList() {
    return Object.keys(this.peers);
  }

  /** 触发 onPeersChange 回调，传递已就绪的 peer 列表 */
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
            sdp: offer.sdp,
            type: offer.type,
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
      this._sendSignal(from, 'screen-answer', {
        sdp: answer.sdp,
        type: answer.type,
      });
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
