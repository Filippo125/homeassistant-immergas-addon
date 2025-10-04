# Modbus Sniffer per Home Assistant
Questo repository contiene un'integrazione custom per Home Assistant che permette di ricevere in tempo reale i frame Modbus sniffati da un convertitore EW11 (in modalità sniffer UDP) e trasformarli in sensori Home Assistant.
## Come funziona
- L'EW11 invia i pacchetti Modbus grezzi via UDP sull'host dove viene eseguita l'integrazione
- L'integrazione apre un listener UDP (predefinito `0.0.0.0:7777`) e ricostruisce i frame Modbus RTU validi.
- Le letture delle holding register (funzione 0x03) e le scritture singole/multiple (0x06/0x10) vengono convertite in aggiornamenti di stato.
- I sensori definiti in configurazione ricevono i valori aggiornati in push, senza necessità di polling.
## Installazione
1. Copia la cartella `custom_components/modbus_sniffer` dentro la cartella `custom_components` della tua installazione Home Assistant.
2. Riavvia Home Assistant per caricare il nuovo componente.
## Configurazione via UI
1. Vai su **Impostazioni → Dispositivi e servizi → Aggiungi integrazione** e cerca "Modbus Sniffer".
2. Inserisci host e porta UDP su cui l'EW11 recapita i pacchetti (default `0.0.0.0:7777`).
3. (Facoltativo) Seleziona la creazione automatica dei sensori di esempio.
4. Dopo la configurazione iniziale puoi aprire le **Opzioni** dell'integrazione per aggiungere, rimuovere o azzerare i sensori tramite l'interfaccia grafica.
## Configurazione di esempio
Aggiungi alla sezione `sensor:` del tuo `configuration.yaml` un blocco come il seguente (adatta nomi, scaling e state map alle tue esigenze):
```yaml
sensor:
  - platform: modbus_sniffer
    udp_port: 7777  # porta su cui l'EW11 invia i pacchetti
    sensors:
      - name: Temperatura esterna
        register: 0x0001
        scale: 0.1
        precision: 1
        unit_of_measurement: "°C"
        device_class: temperature
      - name: Temperatura ritorno
        register: 0x0003
        scale: 0.1
        precision: 1
        unit_of_measurement: "°C"
        device_class: temperature
      - name: Temperatura mandata
        register: 0x0004
        scale: 0.1
        precision: 1
        unit_of_measurement: "°C"
        device_class: temperature
      - name: Temperatura impianto calcolata
        register: 0x0030
        scale: 0.1
        precision: 1
        unit_of_measurement: "°C"
        device_class: temperature
      - name: Stato pompa
        register: 0x003F
        state_map:
          2: "ON"
          21: "OFF"
          22: "AVVIO"
      - name: Setpoint mandata
        register: 0x0005
        scale: 0.1
        precision: 1
        unit_of_measurement: "°C"
```
> ℹ️  L'aggiunta tramite UI e quella YAML possono coesistere: i sensori creati via Opzioni verranno caricati automaticamente, mentre il blocco YAML consente di definire ulteriori sensori avanzati.
### Opzioni supportate per ogni sensore
- `register`: indirizzo della holding register (decimale o esadecimale `0x....`).
- `unit_id`: opzionale, per filtrare solo i frame Modbus di un determinato slave.
- `scale`: fattore moltiplicativo applicato al valore grezzo (default 1.0).
- `offset`: valore sommato dopo lo scaling (default 0.0).
- `precision`: numero di cifre decimali per l'arrotondamento; `null` per lasciare il valore originale.
- `state_map`: mapping numerico→stringa per ottenere uno stato testuale.
- Nella UI inserisci lo `state_map` come coppie `codice=descrizione` separate da virgole o da nuove righe.
- `unit_of_measurement`, `device_class`, `state_class`, `icon`, `force_update`, `device`: opzioni standard dei sensori Home Assistant.
### Registri IMMERGAS AUDAX già osservati
| Registro | Indirizzo | Descrizione | Note |
| -------- | --------- | ----------- | ---- |
| 1        | `0x0001`  | Temperatura esterna | Scala 0.1°C |
| 3        | `0x0003`  | Temperatura ritorno | Scala 0.1°C |
| 4        | `0x0004`  | Temperatura mandata | Scala 0.1°C |
| 5        | `0x0005`  | Setpoint mandata | Scala 0.1°C |
| 48       | `0x0030`  | Temperatura impianto calcolata | Scala 0.1°C |
| 63       | `0x003F`  | Stato impianto | `2=ON`, `21=OFF`, `22=Avvio` |
Puoi arricchire la tabella aggiornando il file `appunti.md` con nuovi registri e scale rilevate durante l'analisi.
## Diagnostica
- Imposta il logger `custom_components.modbus_sniffer` su livello `debug` per vedere i frame riconosciuti:
```yaml
logger:
  default: warning
  logs:
    custom_components.modbus_sniffer: debug
```
- I frame non riconosciuti o i CRC errati vengono scartati;