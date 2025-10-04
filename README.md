# Modbus Register Discovery

Questo repository contiene lo script `udp_web_server.py`, un dashboard web pensato per
ispezionare in tempo reale i frame Modbus RTU e facilitare la discovery dei registri.
Non fa parte dell'integrazione Home Assistant: è uno strumento di supporto da usare
durante l'analisi del bus (ad esempio prima di definire i sensori dell'integrazione
`modbus_sniffer`).

## Caratteristiche principali

- Ascolto dei pacchetti Modbus sia via UDP che come client TCP.
- Decodifica dei frame con evidenza di indirizzo, funzione, payload e CRC.
- Storico dei messaggi recente per i nuovi client collegati e log CSV opzionale.
- Pagina web con aggiornamenti in tempo reale (Server-Sent Events) e filtri per le
  letture/scritture più comuni.

## Prerequisiti

- Python 3.10 o superiore.
- Dipendenze standard della libreria: non sono necessari pacchetti esterni.

## Avvio rapido

Clona o scarica la cartella e lancia lo script in base alla sorgente dati:

### Modalità UDP (default)

```bash
python udp_web_server.py --udp-host 0.0.0.0 --udp-port 7777
```

Lo script apre un listener UDP e attende i datagrammi Modbus (es. da un gateway EW11).

### Modalità TCP client

```bash
python udp_web_server.py --transport tcp --tcp-host 192.168.1.50 --tcp-port 502
```

In questo caso lo script si connette al server TCP indicato, riceve lo stream e cerca di
ricostruire i frame Modbus RTU. In caso di disconnessione tenta automaticamente il
reconnect (configurabile con `--tcp-reconnect-delay`).

Una volta avviato, apri il browser su `http://<host>:8080` (porta configurabile con
`--http-port`) per vedere il dashboard.

## Argomenti principali

| Opzione | Descrizione |
| --- | --- |
| `--transport {udp,tcp}` | Seleziona la sorgente: UDP (default) o client TCP. |
| `--udp-host` / `--udp-port` | Host e porta su cui restare in ascolto dei datagrammi. |
| `--udp-multicast-group` | Indirizzo IPv4 per unirsi a un gruppo multicast. |
| `--tcp-host` / `--tcp-port` | Endpoint del server Modbus/TCP da cui ricevere lo stream. |
| `--buffer-size` | Byte letti per ogni `recv`; influisce anche sul flush del buffer TCP. |
| `--history` | Numero di messaggi mantenuti per nuovi client SSE. |
| `--packet-log` | Percorso del CSV `(timestamp,payload_hex)` popolato in append. |

Usa `python udp_web_server.py --help` per la lista completa delle opzioni.

## Interfaccia web

- **Dashboard in tempo reale**: tabella dei pacchetti ricevuti con dettagli sul payload
  Modbus, note diagnostiche e stato del CRC.
- **History FC03/FC06**: pagine dedicate a letture holding register e scritture singole,
  con filtri per intervallo di registri e timestamp.
- **Download log**: scarica il file CSV generato per ulteriori analisi.

## Suggerimenti per la discovery

1. Avvia il tool mentre il dispositivo Modbus è in funzione e genera traffico reale.
2. Annota gli indirizzi e i valori d'interesse (puoi usare `appunti.md` come taccuino).
3. Quando hai individuato i registri utili, configura l'integrazione principale
   (`custom_components/modbus_sniffer`) in Home Assistant con i registri scoperti.

## Licenza

Non è stata definita una licenza esplicita. Se intendi redistribuire o integrare il
codice in altri progetti, contatta l'autore del repository.
