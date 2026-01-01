# Tabella codifica IMMERGAS Audax 12
| Registro | Descrizione | Note |
| -------- | ----------- | ---- |
|1 (0x0001)| Temperatura esterna | Scala 0.1°C, diminuisce la sera |
|3 (0x0003)| Temperatura ritorno | Scala 0.1°C, sale con caldaia attiva |
|4 (0x0004)| Temperatura mandata | Scala 0.1°C, sale con caldaia attiva |
|5 (0x0005)| Unknown | Sul pannello indicato come 20°C, valori grezzi ~195→ scala 0.1°C |
|30 (0x0030)| Temperatura impianto calcolata | Scala 0.1°C |
|63 (0x003F)| Stato | `1= raffreddamento`, `2=riscaldamento`,`7=sbrinamento`, `21=OFF`, `22= solo circolatore` |
