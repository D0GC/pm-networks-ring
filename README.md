# PM Ring Intercom

Eigene Home-Assistant-Integration für die **Ring Intercom** an der Haustür (Siedle HT 511-01).
Sie erfasst das Klingeln wieder sofort und stellt das **Intercom-Audio** für ein Gespräch über den Browser bereit.

Ausgangspunkt ist die Diagnose vom 07.10.2026: Die offizielle Ring-Integration meldet seit Ende September kein Klingeln der Intercom mehr. Das Entsperren kommt dagegen sekundengenau an.

## Ursache, soweit aus dem Code belegbar

`ring_doorbell` (Version 0.9.14, von HA 2026.9 genutzt) ordnet jede Push-Nachricht über eine feste Tabelle `PUSH_NOTIFICATION_KINDS` zu.

- Eine Kategorie, die nicht in der Tabelle steht, wird zu `"Unknown"`. Die offizielle Integration verwirft sie still.
- Eine Nachricht mit abweichendem Aufbau, etwa ohne `event.ding`, löst einen `KeyError` aus und geht ebenfalls verloren.
- Das Entsperren kommt über das alte `gcmData`-Format mit fester Aktion. Deshalb funktioniert es weiterhin.

Das neue Merkmal `ding_call` in der Diagnose spricht dafür, dass Ring das Klingeln inzwischen als Anruf mit neuer Kategorie zustellt. Endgültig belegt ist das erst mit dem ersten Mitschnitt (siehe *Diagnose*).

## Funktionsweise

1. **Eigener Push-Empfang.** Die Integration meldet sich als eigener Client bei Ring an („PMNetwork-HomeControl“ im Control Center) und registriert ein eigenes FCM-Token. Jede Rohnachricht wird tolerant ausgewertet. Alles mit *ding*, *intercom* oder *call* in Kategorie oder Subtyp gilt als Klingeln, *unlock* und *motion* nicht.
2. **Abfrage als Rückfallebene.** Alle 15 Sekunden (einstellbar) wird die Historie der Intercom gelesen. Ein Klingeln, das per Push ausblieb, wird so spätestens nach etwa 15 Sekunden gemeldet. Die offizielle Integration braucht dafür rund 60 Sekunden.
3. **Entprellung.** Push und Abfrage melden dasselbe Klingeln nur einmal (über die Ding-ID und ein Zeitfenster von 90 Sekunden).

## Entitäten

Sie hängen über die MAC-Adresse am bestehenden Gerät „Haustür“.

| Entität (voraussichtliche ID) | Zweck |
|---|---|
| `event.haustur_klingeln_live` | Ereignis `ring` bei jedem Klingeln. Die Attribute nennen Quelle (`push`, `abfrage`, `test`) und Verzögerung. |
| `binary_sensor.haustur_klingelt` | Nach dem Klingeln 30 Sekunden lang `on`. Geeignet für PM Panel Studio (`ereignis_ausloeser`). |
| `sensor.haustur_intercom_audio` | `bereit` oder `gespraech`. |
| `button.haustur_tur_offnen` | Öffnet die Haustür über die Intercom. Übernimmt die ID des Knopfs der offiziellen Ring-Integration. |
| `binary_sensor.haustur_push_verbindung` | Diagnose: Push-Empfang aktiv, Zähler, Abfragestatus. |
| `sensor.haustur_letzte_push_meldung` | Diagnose: Kategorie der zuletzt empfangenen Ring-Nachricht. |

Zusätzlich gibt es das Bus-Ereignis `pm_ring_intercom_ding` und den Dienst `pm_ring_intercom.klingeln_simulieren`.

## Installation

1. HACS → Benutzerdefinierte Repositories → `https://github.com/D0GC/pm-networks-ring`, Typ *Integration*.
   Alternativ den Ordner `custom_components/pm_ring_intercom` nach `/config/custom_components/` kopieren.
2. Home Assistant neu starten.
3. Einstellungen → Geräte & Dienste → *PM Ring Intercom* hinzufügen. Mit dem Ring-Konto anmelden und den Bestätigungscode eingeben.

Die offizielle Ring-Integration bleibt für die übrigen Ring-Geräte bestehen. Für die Intercom liefert sie nur noch Lautstärken, Batterie und Signal; Klingeln und Türöffner kommen aus PM Ring Intercom.

## Umstellung von VisioGong

Empfohlen ist ein Zustandsauslöser auf das neue Ereignis:

```yaml
triggers:
  - trigger: state
    entity_id: event.haustur_klingeln_live
    not_from: unavailable
conditions:
  - condition: state
    entity_id: event.haustur_klingeln_live
    attribute: event_type
    state: ring
```

Eine Prüfung ohne echtes Klingeln ist mit `pm_ring_intercom.klingeln_simulieren` möglich.

## Intercom-Audio (experimentell)

Ring baut Live-Gespräche über einen eigenen WebRTC-Signalisierungsdienst auf (`live_view`). `ring_doorbell` nutzt ihn nur für Kameras und fordert dort immer Video an. Die Integration fordert für die Intercom **nur Audio** an. Der Ton läuft direkt zwischen Browser und Ring, Home Assistant vermittelt nur den Verbindungsaufbau.

**Ob Ring eine solche Sitzung für die Intercom annimmt, ist nicht belegt.** Die Ring-App bietet für die Intercom Live-Audio an, und das Gerät hat Mikrofon- und Sprachlautstärke. Die Schnittstelle ist dieselbe. Der erste Versuch zeigt es. Die gesamte Signalisierung wird mitgeschnitten (Diagnose-Download, Abschnitt `audio_signalisierung`).

### Karte

Die Integration liefert die Karte `custom:pm-intercom-card` mit, eine Ressource ist nicht nötig.

```yaml
type: custom:pm-intercom-card
titel: Haustür
klingelt: binary_sensor.haustur_klingelt
tueroeffner: button.haustur_tur_offnen
```

Knöpfe: *Sprechen*, *Stumm*, *Auflegen* und *Tür öffnen* (2 Sekunden halten).

**Voraussetzung:** Browser geben das Mikrofon nur in einem sicheren Kontext frei, also über HTTPS (Nabu Casa, Let's Encrypt) oder `localhost`. Über `http://homeassistant.local:8123` oder `http://…:8098` bleibt das Mikrofon gesperrt. Das Wandpanel braucht ein Gerät mit Mikrofon und Lautsprecher. Das NSPanel hat beides nicht.

### WebSocket-Schnittstelle (für PM Panel Studio)

| Befehl | Daten | Antwort |
|---|---|---|
| `pm_ring_intercom/audio/start` | `offer` (SDP des Browsers mit Mikrofonspur), optional `entry_id` | Abonnement. Ereignisse: `session` (`session_id`), `answer` (`answer`), `candidate` (`candidate.candidate`, `candidate.sdpMLineIndex`), `error` (`code`, `message`), `closed`. Abbestellen beendet das Gespräch. |
| `pm_ring_intercom/audio/candidate` | `session_id`, `candidate`, `sdp_m_line_index` | Ergebnis ohne Daten. |

Die Karte in `custom_components/pm_ring_intercom/www/pm-intercom-card.js` ist die Referenzimplementierung.

## Diagnose

- `sensor.haustur_letzte_push_meldung` zeigt nach dem nächsten Klingeln die tatsächliche Kategorie.
- *Diagnose herunterladen* am Integrationseintrag enthält die letzten 20 Push-Nachrichten (geschwärzt) und den Mitschnitt der Audio-Signalisierung.
- Ausführliches Protokoll:

```yaml
logger:
  logs:
    custom_components.pm_ring_intercom: debug
```

Meldet `binary_sensor.haustur_push_verbindung` das Klingeln dauerhaft nur unter `klingeln_nur_per_abfrage`, stellt Ring der Intercom keinen Push für diesen Client zu. Dann trägt die Abfrage allein.

## Entwicklung

```bash
pip install pytest-homeassistant-custom-component
pytest tests
```
