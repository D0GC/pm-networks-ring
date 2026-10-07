/*
 * PM Intercom Card – Intercom-Audio der Ring Intercom (experimentell).
 *
 * type: custom:pm-intercom-card
 * klingelt: binary_sensor.haustur_klingelt    # optional, hebt die Karte hervor
 * tueroeffner: button.haustur_tur_offnen      # optional, Auslösen nach 2 s Halten
 * titel: Haustür                              # optional
 * entry_id: …                                 # nur bei mehreren Intercoms
 * ice_servers: [{urls: "stun:stun.l.google.com:19302"}]   # optional
 *
 * Das Mikrofon funktioniert nur in einem sicheren Kontext (HTTPS oder localhost).
 */

const VERSION = "1.0.0";

class PmIntercomCard extends HTMLElement {
  setConfig(config) {
    this._config = config || {};
    this._state = "bereit";
    this._muted = false;
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    this._updateRinging();
  }

  getCardSize() {
    return 3;
  }

  static getStubConfig() {
    return { klingelt: "binary_sensor.haustur_klingelt", tueroeffner: "button.haustur_tur_offnen" };
  }

  _render() {
    if (!this.shadowRoot) this.attachShadow({ mode: "open" });
    const titel = this._config.titel || "Haustür";
    this.shadowRoot.innerHTML = `
      <style>
        ha-card { padding: 16px; transition: box-shadow .3s; }
        ha-card.ringing { box-shadow: 0 0 0 3px var(--warning-color, #ffa600); }
        .kopf { display:flex; align-items:center; justify-content:space-between; }
        .titel { font-size: 1.2em; font-weight: 500; }
        .status { color: var(--secondary-text-color); margin: 8px 0 16px; min-height: 1.2em; }
        .knoepfe { display:flex; gap: 8px; flex-wrap: wrap; }
        button { flex: 1 1 30%; min-height: 56px; border: 0; border-radius: 12px;
                 font-size: 1em; cursor: pointer; color: var(--text-primary-color, #fff);
                 background: var(--primary-color); }
        button[hidden] { display: none; }
        button.auflegen { background: var(--error-color, #db4437); }
        button.stumm.aktiv { background: var(--disabled-text-color, #888); }
        button.tuer { background: var(--success-color, #43a047); }
        button.tuer.halten { filter: brightness(1.3); }
      </style>
      <ha-card>
        <div class="kopf"><span class="titel"></span><span class="pegel"></span></div>
        <div class="status"></div>
        <div class="knoepfe">
          <button class="annehmen">Sprechen</button>
          <button class="stumm" hidden>Stumm</button>
          <button class="auflegen" hidden>Auflegen</button>
          <button class="tuer" hidden>Tür öffnen (halten)</button>
        </div>
        <audio autoplay playsinline></audio>
      </ha-card>`;
    const root = this.shadowRoot;
    root.querySelector(".titel").textContent = titel;
    root.querySelector(".annehmen").addEventListener("click", () => this._start());
    root.querySelector(".auflegen").addEventListener("click", () => this._stop("Gespräch beendet."));
    root.querySelector(".stumm").addEventListener("click", () => this._toggleMute());
    const tuer = root.querySelector(".tuer");
    tuer.hidden = !this._config.tueroeffner;
    const startHold = (ev) => {
      ev.preventDefault();
      tuer.classList.add("halten");
      this._holdTimer = setTimeout(() => this._openDoor(), 2000);
    };
    const endHold = () => {
      tuer.classList.remove("halten");
      clearTimeout(this._holdTimer);
    };
    tuer.addEventListener("pointerdown", startHold);
    ["pointerup", "pointerleave", "pointercancel"].forEach((e) => tuer.addEventListener(e, endHold));
    this._setStatus(this._secureHint() || "Bereit.");
  }

  _secureHint() {
    if (!window.isSecureContext || !navigator.mediaDevices) {
      return "Mikrofon gesperrt: Die Seite muss über HTTPS geöffnet werden.";
    }
    return "";
  }

  _setStatus(text) {
    const el = this.shadowRoot && this.shadowRoot.querySelector(".status");
    if (el) el.textContent = text;
  }

  _setActive(active) {
    const r = this.shadowRoot;
    r.querySelector(".annehmen").hidden = active;
    r.querySelector(".auflegen").hidden = !active;
    r.querySelector(".stumm").hidden = !active;
  }

  _updateRinging() {
    if (!this._hass || !this.shadowRoot) return;
    const id = this._config.klingelt;
    const on = id && this._hass.states[id] && this._hass.states[id].state === "on";
    this.shadowRoot.querySelector("ha-card").classList.toggle("ringing", !!on);
    if (on && !this._pc) this._setStatus("Es klingelt.");
  }

  async _start() {
    if (this._pc) return;
    const hint = this._secureHint();
    if (hint) {
      this._setStatus(hint);
      return;
    }
    this._setActive(true);
    this._setStatus("Verbinde …");
    this._sessionId = null;
    this._pendingLocal = [];
    this._pendingRemote = [];
    try {
      this._mic = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
        video: false,
      });
      const pc = new RTCPeerConnection({
        iceServers: this._config.ice_servers || [{ urls: "stun:stun.l.google.com:19302" }],
      });
      this._pc = pc;
      this._mic.getTracks().forEach((t) => pc.addTransceiver(t, { direction: "sendrecv", streams: [this._mic] }));
      if (this._config.video) pc.addTransceiver("video", { direction: "recvonly" });

      pc.ontrack = (ev) => {
        const audio = this.shadowRoot.querySelector("audio");
        audio.srcObject = ev.streams[0] || new MediaStream([ev.track]);
        audio.play().catch(() => {});
      };
      pc.onicecandidate = (ev) => {
        if (!ev.candidate) return;
        const c = ev.candidate.toJSON();
        if (this._sessionId) this._sendCandidate(c);
        else this._pendingLocal.push(c);
      };
      pc.onconnectionstatechange = () => {
        const s = pc.connectionState;
        if (s === "connected") this._setStatus("Verbunden. Sie können sprechen.");
        if (s === "failed") this._stop("Verbindung fehlgeschlagen.");
        if (s === "disconnected") this._setStatus("Verbindung unterbrochen …");
      };

      const offer = await pc.createOffer();
      await pc.setLocalDescription(offer);

      this._unsub = await this._hass.connection.subscribeMessage(
        (msg) => this._onMessage(msg),
        this._withEntry({ type: "pm_ring_intercom/audio/start", offer: offer.sdp })
      );
    } catch (err) {
      this._stop(`Fehler: ${err && err.message ? err.message : err}`);
    }
  }

  async _onMessage(msg) {
    const pc = this._pc;
    if (!pc) return;
    try {
      if (msg.type === "session") {
        this._sessionId = msg.session_id;
        const queued = this._pendingLocal.splice(0);
        queued.forEach((c) => this._sendCandidate(c));
      } else if (msg.type === "answer") {
        await pc.setRemoteDescription({ type: "answer", sdp: msg.answer });
        const queued = this._pendingRemote.splice(0);
        for (const c of queued) await pc.addIceCandidate(c);
        this._setStatus("Ring hat geantwortet, verbinde Audio …");
      } else if (msg.type === "candidate") {
        const c = { candidate: msg.candidate.candidate, sdpMLineIndex: msg.candidate.sdpMLineIndex ?? 0 };
        if (pc.remoteDescription) await pc.addIceCandidate(c);
        else this._pendingRemote.push(c);
      } else if (msg.type === "error") {
        this._stop(`Ring: ${msg.message || msg.code}`);
      } else if (msg.type === "closed") {
        this._stop("Ring hat das Gespräch beendet.");
      }
    } catch (err) {
      console.warn("pm-intercom-card", err);
    }
  }

  _sendCandidate(c) {
    this._hass
      .callWS(
        this._withEntry({
          type: "pm_ring_intercom/audio/candidate",
          session_id: this._sessionId,
          candidate: c.candidate,
          sdp_m_line_index: c.sdpMLineIndex ?? 0,
        })
      )
      .catch((e) => console.warn("pm-intercom-card candidate", e));
  }

  _withEntry(msg) {
    if (this._config.entry_id) msg.entry_id = this._config.entry_id;
    return msg;
  }

  _toggleMute() {
    if (!this._mic) return;
    this._muted = !this._muted;
    this._mic.getAudioTracks().forEach((t) => (t.enabled = !this._muted));
    const b = this.shadowRoot.querySelector(".stumm");
    b.classList.toggle("aktiv", this._muted);
    b.textContent = this._muted ? "Mikrofon an" : "Stumm";
  }

  _stop(text) {
    if (this._unsub) {
      try { this._unsub(); } catch (e) { /* bereits geschlossen */ }
    }
    this._unsub = null;
    if (this._pc) this._pc.close();
    this._pc = null;
    if (this._mic) this._mic.getTracks().forEach((t) => t.stop());
    this._mic = null;
    this._muted = false;
    const b = this.shadowRoot.querySelector(".stumm");
    b.classList.remove("aktiv");
    b.textContent = "Stumm";
    this._setActive(false);
    this._setStatus(text || "Bereit.");
  }

  _openDoor() {
    const id = this._config.tueroeffner;
    if (!id || !this._hass) return;
    const domain = id.split(".")[0];
    const service = domain === "lock" ? "unlock" : "press";
    this._hass.callService(domain, service, { entity_id: id });
    this._setStatus("Tür wird geöffnet.");
  }

  disconnectedCallback() {
    if (this._pc) this._stop();
  }
}

if (!customElements.get("pm-intercom-card")) {
  customElements.define("pm-intercom-card", PmIntercomCard);
  window.customCards = window.customCards || [];
  window.customCards.push({
    type: "pm-intercom-card",
    name: "PM Intercom",
    description: "Intercom-Audio der Ring Intercom und Tür öffnen.",
  });
  console.info(`PM-INTERCOM-CARD ${VERSION}`);
}
