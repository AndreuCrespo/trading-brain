# MCPDataExport — estudio ACSIL de export a JSON

Estudio custom de Sierra Chart (escrito por el Claude de donAdri) que vuelca en cada tick
los valores de los estudios visuales del gráfico a un JSON plano
(`sc_data.json`), para que un agente externo los lea sin DTC.

## Instalación
1. Copiar `MCPDataExport.cpp` a `C:\SierraChart\ACS_Source\`
2. Sierra Chart → Analysis → Build Custom Studies DLL
3. Añadir el estudio "MCP Data Export" al gráfico principal (región 0)
4. Configurar en los inputs la ruta de salida y los **Study IDs de TU gráfico**
   (los defaults son los IDs del chart de donAdri — hay que recablearlos)

## Cómo lee los valores
`GetStudyArrayFromChartUsingID(chart, studyID, subgraph)` sobre los estudios:
ECIVwapV1.5 (DVA ETH), rthVWAP (DVA RTH), Volume Value Area Lines (pVA),
Volume Profile RTH (VA actual), IB High/Low, ADR Bands, Cumulative Delta,
Daily OHLC, wVWAP, mVWAP, ONH/ONL. Además clasifica: IMBALANCEADO/ROTACIONAL
(precio vs 1ª desviación), DENTRO/FUERA de pVA, estado del IB, dirección del delta.

`sc_data.sample.json` es un snapshot real de ejemplo (ES, sesión 2026-07-13).

## ⚠️ Campos NO fiables (no calibrar contra ellos)
1. **`dva_eth.desv2_*` y `desv3_*`**: los subgraphs de los estudios de referencia
   (Inputs 5-6) están desordenados — la 2ª desviación sale MENOR que la 1ª.
   Pendiente de recablear IDs. La 1ª desviación (`desv1_*`) y el `fsvwap` SÍ son correctos.
2. **`va_actual_rth`**: el `poc` devuelve un valor basura (p.ej. 24.00) y VAH/VAL
   salen a veces invertidos — el Study ID del Input 11 no apunta al subgraph correcto.
3. **`dva_rth.*` e `ib.*` valen 0 en premarket** (antes de la apertura RTH) — normal,
   pero no interpretar el 0 como nivel.

Campos validados en vivo contra el chart: `fsvwap`, `desv1_*` (ETH y RTH),
`pva` completo, `ib` en horario RTH, `adr`, `delta`, `niveles` (wVWAP/mVWAP/ONH/ONL/
pHOD/pLOD/pClose/day_open), y las clasificaciones `condicion`/`precio_vs_*`.

## Calibración imbalanceado/rotacional (ojo de donAdri, 2026-07-13)
- Lun 13-jul ~16:00 ES (VWAP +0.47× la 1ª desv en 3h, precio cruzando ambos lados) → **ROTACIONAL**
- Vie 10-jul ~21:00 ES (precio cabalgando +1σ/+1.8σ sin re-entrar) → **IMBALANCEADO**
- Mar 7-jul ~20:00 ES (VWAP −0.27×σ/1.5h, precio cruzando) → **ROTACIONAL**

Conclusión para el etiquetador: **la pendiente sola no basta** — criterio doble:
pendiente ≥ ~0.6-0.7× la 1ª desv en 3h **O** precio persistiendo más allá de la
1ª desviación sin re-entrar al valor. Si el precio cruza el VWAP por ambos lados,
es rotacional aunque haya deriva.
