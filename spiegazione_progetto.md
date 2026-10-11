# Guida al percorso dei dati nel backend

## Come leggere il progetto

Non iniziare leggendo i file dall’alto al basso. Prima ricostruisci il percorso dei dati; poi segui le funzioni che lo realizzano. Il backend è relativamente piccolo (circa sette file Python e 1.900 righe), ma la gestione dei resi richiede particolare attenzione.

Questa guida descrive tre componenti distinti:

```text
Cliente
  ↓
Chatbot indigo.ai — gestisce conversazione, contesto e scelta dei tool
  ↓ parametri strutturati
Backend Python / FastAPI
  ├─ Catalogo CSV: descrizioni e caratteristiche
  ├─ Azure embeddings / Jev: pertinenza della ricerca
  ├─ OrdiNet SOAP: prezzi, disponibilità e ordini
  └─ Azure Table: stato persistente dei resi
```

Il backend non è il chatbot e, normalmente, non scrive una risposta conversazionale. Espone operazioni strutturate e restituisce JSON che l’agente deve interpretare. OrdiNet è il gestionale esterno; Azure Table conserva i dati dei resi che devono sopravvivere ai riavvii.

## 1. I quattro strumenti esposti all’agente

| Tool | Endpoint | Scopo |
|---|---|---|
| `get_catalog_options` | `GET /catalog/options` | Scoprire etichette e valori del catalogo |
| `search_products` | `POST /products` | Cercare prodotti o leggere dettagli per ID |
| `get_order_status` | `POST /orders/status` | Verificare un ordine |
| `manage_return` | `POST /returns` | Proporre, confermare, annullare o consultare un reso |

Le modalità `semantic`, `hybrid` e `jev` non sono tool distinti: sono valori di un parametro di ricerca.

## 2. `models.py`: il contratto dei dati

Parti dai modelli Pydantic. Definiscono quali dati l’API accetta e restituisce; Pydantic applica davvero i vincoli durante la validazione.

Esempio:

```python
mode: Literal["semantic", "hybrid", "jev"] = "semantic"
k: int = Field(default=5, ge=1, le=10)
```

`mode` accetta solo i tre valori indicati e, se omesso, vale `semantic`. `k` è un intero da 1 a 10 e, se omesso, vale 5.

Modelli da individuare:

- `CatalogOptionsRequest`: richiesta di etichette.
- `ProductFilters`: parametri e filtri della ricerca.
- `SearchResults`: risposta della ricerca.
- `OrderRequest`: numero ordine ed email.
- `OrderStatus`: stato ordine normalizzato.
- `Error`: formato comune degli errori.
- Gli schemi specifici dei resi si trovano in `returns.py`.

Tre dettagli contano molto:

1. **`extra="forbid"`** rifiuta i campi non previsti. Per esempio, l’agente non può aggiungere `confirmed=true` a una richiesta che non lo ammette.
2. **`validate_ranges()`** blocca intervalli incoerenti prima di chiamare servizi esterni: prezzo minimo 50 € e massimo 20 € è una richiesta non valida.
3. **Campo assente e `null` hanno significati diversi.** Un campo assente non è stato richiesto nell’output; un campo richiesto con valore `null` indica che il dato manca o non è verificato.

`fields=["name"]` sceglie quali dati mostrare; non deve disattivare il filtro sul prezzo. Selezione dell’output e controlli di ricerca sono responsabilità diverse.

## 3. `catalog.py`: cosa arriva dal CSV

La classe centrale è `Catalog`. All’avvio carica il CSV e prepara strutture in memoria.

- `details(product_id)` converte una riga CSV in dati utilizzabili: testo in stringhe, `yes` in booleani, tag separati da `|` in liste, numeri in valori numerici e celle mancanti in `None`.
- `matches(product, filters)` applica i filtri statici alle caratteristiche del prodotto.
- `options(filters)` elenca valori come categorie, marche e colori, con il numero di prodotti che hanno ciascun valore.
- `search(filters)` esegue il ranking lessicale BM25.
- `metadata_text(product)` costruisce il testo usato dalla ricerca, dando peso maggiore ad alcuni campi tramite ripetizione: nome tre volte, tag due volte, poi occasioni, categoria, marca, descrizione, materiale e colore.

La logica dei filtri è **OR tra i valori dello stesso filtro** e **AND tra filtri diversi**. Quindi `color=["black", "white"]` insieme a `gift_wrap=true` significa nero oppure bianco, e in entrambi i casi confezione regalo.

I prezzi e le quantità presenti nel CSV non sono fonte autorevole per l’acquisto: il CSV descrive i prodotti; il gestionale fornisce prezzi e disponibilità correnti. Anche i conteggi di `options()` indicano prodotti che hanno un’etichetta, non prodotti acquistabili.

## 4. `retrieval.py`: come si ordinano i prodotti

La classe `SearchEngine` combina filtri e ranking. Il percorso è:

```text
Filtri statici → candidati compatibili → modalità di ranking → ID ordinati e punteggi
```

La funzione centrale è `rank()`.

### Modalità `semantic`

1. Invia la query ad Azure per calcolarne l’embedding, una rappresentazione numerica del significato.
2. Confronta il vettore della query con i vettori dei prodotti già salvati in `data/search-vectors.json`.
3. Ordina i candidati per similarità.

I vettori dei prodotti non vengono rigenerati a ogni ricerca. `unit()` normalizza i vettori; il prodotto scalare successivo consente di calcolare la similarità coseno.

### Modalità `hybrid`

Combina ranking BM25 e ranking semantico con RRF (Reciprocal Rank Fusion). RRF combina le posizioni nelle classifiche, non i punteggi originali. Nel codice, ogni lista contribuisce con `0.5 / (60 + rank)`. Un prodotto ben posizionato in entrambe riceve contributi da entrambe. `hybrid` non è automaticamente migliore in ogni caso.

### Modalità `jev`

Invia a TypeSafe la query, i metadati dei candidati e una domanda strutturata per ciascuno. In sostanza chiede se il prodotto corrisponde all’oggetto, allo scopo e alle caratteristiche richieste. Riceve un punteggio da 0 a 1 per candidato, non una risposta da mostrare al cliente.

### Protezioni operative

- `fingerprint()` verifica la coerenza tra catalogo e indice vettoriale.
- `_cached()` riusa inferenze recenti.
- Richieste identiche contemporanee condividono lo stesso lavoro.
- Sono ammesse al massimo quattro inferenze nuove contemporanee.
- La cache contiene al massimo 256 elementi, validi per 10 minuti.
- Le risposte dei provider vengono validate.
- Gli errori non vengono nascosti con fallback silenziosi.

Questa cache riguarda le inferenze: **non** conserva prezzi o disponibilità SOAP.

Punto essenziale: **il ranking ordina, i filtri escludono**. Una query come “contenitore per tenere caldo il pranzo” può lasciare in fondo risultati poco pertinenti. La posizione in classifica non dimostra che un prodotto soddisfi davvero lo scopo: l’agente deve verificare le caratteristiche.

## 5. `tools.py`: dalla richiesta all’operazione

Segui prima `search_products()`. Per esempio:

```json
{
  "query": "un contenitore per tenere caldo il pranzo",
  "max_price_eur": 40,
  "available_only": true,
  "k": 3
}
```

Il flusso è:

1. Autenticare la chiamata e validare i dati.
2. Controllare categoria e query.
3. Applicare i filtri statici e ordinare i candidati.
4. Chiedere a SOAP prezzi e disponibilità per un massimo di 20 candidati.
5. Applicare i vincoli su prezzo, stock e disponibilità reale.
6. Unire metadati del catalogo e dati live.
7. Selezionare i campi richiesti.
8. Restituire i prodotti e `next_offset`.

Il tetto di prezzo si applica dopo la lettura SOAP perché il prezzo aggiornato non è noto prima.

### Paginazione

`offset` indica la posizione nell’elenco dei candidati, non il numero di prodotti già convalidati. È possibile controllare 20 candidati e non trovarne nessuno acquistabile entro il budget, pur avendo altri candidati da esaminare. Perciò una risposta con `products: []` e `next_offset: 20` non significa necessariamente che la ricerca sia terminata.

Il vecchio `GET /products` resta per compatibilità, ma è nascosto nello schema OpenAPI.

Altri punti di `tools.py`:

- `authenticate()` controlla `X-API-Key`.
- `ToolError` rappresenta un errore sicuro da restituire all’agente.
- Gli error handler normalizzano errori di validazione e del gestionale.
- `redact_log()` oscura dati sensibili nelle copie destinate ai log.
- `get_order_status()` legge l’ordine e confronta l’email prima di restituire informazioni.

La API key autentica la connessione del tool; **non** autentica il cliente.

## 6. `ordinet.py`: il traduttore SOAP

`ordinet.py` nasconde al resto del progetto la comunicazione con OrdiNet. Il backend usa dati Python; il gestionale usa XML/SOAP.

```text
Dati Python → XML SOAP → OrdiNet → XML di risposta → dati Python normalizzati
```

Funzioni da seguire:

- `_exchange()`: costruisce e invia la richiesta SOAP.
- `_login()`: ottiene e riusa una sessione.
- `_read()`: gestisce recupero della sessione e retry delle letture.
- `availability()`: legge prezzi, quantità e disponibilità.
- `order()`: legge un ordine.
- `create_return()`: crea realmente un reso.

Il client normalizza codici in stati leggibili (per esempio `ST05` → `delivered`, `ST07` → `return_in_progress`, `01` → `available`) e converte date in ISO (`07/10/2026` → `2026-10-07`). Usa `Decimal` per i prezzi, evitando gli errori di precisione tipici dei `float`. Il parser XML usa `defusedxml` contro XML pericoloso.

### Letture e scritture non sono uguali

Le letture possono essere ritentate. La creazione del reso non viene ritentata automaticamente: il gestionale potrebbe averlo creato anche se la risposta si è persa, e una seconda richiesta potrebbe creare un duplicato. Questa scelta determina il flusso di sicurezza dei resi.

## 7. `returns.py`: macchina a stati dei resi

È la parte più complessa. Studiala per fasi, non in sequenza dall’inizio alla fine.

| Operazione | Effetto |
|---|---|
| `options` | Legge gli articoli e verifica l’ammissibilità |
| `prepare` | Salva una proposta e il relativo riepilogo |
| `approve` | Verifica il consenso e, se abilitato, prova a inviare |
| `cancel` | Annulla una proposta non inviata |
| `status` | Legge lo stato locale e quello dell’ordine |

Gli stati seguono questo percorso:

```text
nessuna proposta → prepared
                     ├─ cancel → cancelled
                     └─ approve → submitting
                                    ├─ successo verificato → submitted
                                    └─ esito incerto → submission_unknown
```

Dopo un annullamento è possibile preparare una nuova proposta, con una nuova richiesta e conservando la storia precedente.

### Cinque controlli da comprendere

**A. Verifica dell’ordine.** Ogni operazione confronta numero ordine ed email con i dati SOAP. Il browser o la provenienza della richiesta non autorizzano l’operazione.

**B. Ammissibilità.** `eligible()` controlla che l’ordine risulti consegnato, che la data di consegna sia verificabile e rientri nella finestra documentata di 30 giorni, che gli articoli siano nell’ordine e che le quantità richieste non superino quelle acquistate.

**C. Proposta precisa.** `prepare()` salva articoli e quantità, motivo, riepilogo, `draft_id`, scadenza di 15 minuti, modalità prova/reale e ID del messaggio che ha preparato la proposta. Se la proposta cambia, cambia anche il riferimento che l’utente deve confermare.

**D. Consenso in un nuovo messaggio.** `event()` legge il messaggio originale dagli header della piattaforma (`X-Chat-User-Message-B64` e `X-Chat-Message-Id`). Il JSON prodotto dal modello non può dichiarare da solo che il cliente ha confermato. Per approvare serve il messaggio esatto `CONFERMO`; per annullare serve `ANNULLA`. Non sono accettate parafrasi o spazi aggiuntivi.

Base64 è una codifica, non una firma crittografica. La fiducia dipende anche dalla corretta configurazione della connessione.

**E. Un solo concorrente può inviare.** `TableStore` salva una riga per ordine in Azure Table. L’ETag identifica la versione della riga. Una modifica riesce solo se la versione è ancora quella letta: se due richieste leggono la versione 7, una può salvare `submitting` come versione 8; l’altra non può modificare la vecchia versione 7. È un controllo CAS (compare-and-swap). Solo chi acquisisce e verifica il claim persistente può procedere.

### Perché esiste `submission_unknown`

OrdiNet potrebbe avere creato il reso senza che il backend abbia ricevuto la risposta. In questo caso il flusso si ferma e richiede verifica manuale, invece di rischiare un secondo invio.

Non è una garanzia globale di “exactly once” da parte del gestionale: protegge dai nuovi tentativi automatici del nostro flusso. Inoltre, `ST07` indica soltanto che esiste un reso in corso; non identifica necessariamente quello creato da questa richiesta.

## 8. `main.py`: avvio e composizione

Dopo aver seguito i flussi, `main.py` dovrebbe essere più facile da leggere. La funzione `lifespan()`:

1. carica la configurazione e controlla le credenziali obbligatorie;
2. carica il catalogo;
3. crea i client HTTP;
4. crea la ricerca e il client OrdiNet;
5. collega lo storage resi, se abilitato;
6. libera le risorse alla chiusura.

Gli oggetti condivisi sono in `app.state.catalog`, `app.state.search`, `app.state.ordinet` e `app.state.returns`. Poi vengono registrati router, gestione errori e pagina statica.

Il middleware dei log registra nome del tool, parametri oscurati, esito e ID di correlazione. Compatta solo il log, non la risposta inviata all’agente. `/health` verifica che il processo risponda; non certifica che SOAP o i provider AI siano disponibili.

## 9. Il resto del repository

- **`frontend/index.html`**: landing statica in HTML, CSS e JavaScript. Carica il widget indigo.ai solo dopo un click. Non è una seconda implementazione del chatbot e non deve contenere la API key del backend.
- **`docs/agent-prompts/agent-goal.txt`**: definisce quando cercare, quali vincoli mantenere, quando chiedere chiarimenti e come gestire conferme e risultati incerti. Prompt e controlli Python sono livelli distinti: le regole critiche non devono dipendere solo dall’obbedienza del modello.
- **`scripts/prepare_search_vectors.py`**: rigenera gli embedding del catalogo e può generare costi.
- **`scripts/evaluate_search.py`**: esamina il benchmark; chiama provider solo con `--online`.
- **`Dockerfile`**: usa Python 3.13, installa dipendenze, copia gli asset e avvia Uvicorn come utente non privilegiato. I segreti arrivano a runtime e non vengono copiati nell’immagine.
- **`assignment/` e `consegna/`**: materiali di ricerca, esperimenti e consegna; non sono il percorso runtime principale.

