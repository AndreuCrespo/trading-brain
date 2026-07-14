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
Daily OHLC, wVWAP, mVWAP, ONH/ONL. Además clasifica: posición vs 1ª desviación
(`FUERA_DESV1`/`DENTRO_DESV1`), DENTRO/FUERA de pVA, estado del IB y dirección
del delta. Cuando un estudio no da valores válidos (p.ej. premarket), las
clasificaciones exportan `SIN_DATOS`/`NO_FIABLE` en vez de una etiqueta falsa.

`sc_data.sample.json` es un snapshot real de ejemplo (ES, sesión 2026-07-13),
**anterior a los arreglos de la revisión** — conserva a propósito los síntomas
descritos abajo (prev_hod < prev_lod, pct_completado de una sola barra, etc.).

## Cambios tras la revisión de la PR (2026-07-14)
- **Mapeo Daily OHLC corregido**: era `0=pHOD, 1=pLOD, 2=pClose, 3=Open` y el
  sample demostraba el orden real `0=Open, 1=High, 2=Low, 3=Close` (salía
  prev_hod < prev_lod). Re-validar en vivo tras recompilar.
- **`adr.pct_completado`** ahora usa el rango real de la sesión (recorre las
  barras del trading day); antes usaba el high-low de la última barra (~2.65%
  en un día que llevaba ~53% del ADR).
- **`condicion` renombrada a `FUERA_DESV1`/`DENTRO_DESV1`** ⚠ breaking change:
  el nombre anterior (IMBALANCEADO/ROTACIONAL) prometía el criterio calibrado
  del playbook, pero el código solo mira la posición instantánea del precio —
  el propio sample del 13-jul decía IMBALANCEADO en un día etiquetado
  "claramente rotacional". El imbalance calibrado (pendiente + persistencia)
  vive en `indicator_engine.dva_state` del sierra-mcp.
- **Escritura atómica** (tmp + `MoveFileEx`): antes un lector podía pillar el
  JSON vacío o cortado a mitad de escritura.
- **Throttle configurable** (Input 17, default 1 s) en vez de escribir en cada tick.
- **Guardas de datos inválidos** en todas las clasificaciones (`SIN_DATOS`).
- JSON con locale clásico (punto decimal garantizado) y `relative_volume` sobre
  las 10 barras cerradas (la barra en formación infravaloraba el RVOL; en charts
  de barras de volumen este campo mide fracción de barra completada, no RVOL).

## ⚠️ Campos NO fiables (no calibrar contra ellos)
1. **`dva_eth.desv2_*` y `desv3_*`**: los subgraphs de los estudios de referencia
   (Inputs 5-6) están desordenados — la 2ª desviación sale MENOR que la 1ª.
   Pendiente de recablear IDs. La 1ª desviación (`desv1_*`) y el `fsvwap` SÍ son correctos.
2. **`dva_rth.desv2_*` y `desv3_*`** (detectado en la revisión): mismo síntoma que
   en ETH — en el sample la `desv2_arriba` (7575.87) queda POR DEBAJO del vwap
   (7588.22) y la 3ª sale invertida. Pendiente de recablear Inputs 3-4.
3. **`va_actual_rth`**: el `poc` devuelve un valor basura (p.ej. 24.00) y VAH/VAL
   salen a veces invertidos — el Study ID del Input 11 no apunta al subgraph correcto.
   (La clasificación derivada ahora emite `NO_FIABLE` si VAH/VAL llegan incoherentes.)
4. **`niveles.onh/onl`** (detectado en la revisión): el sample da ONH=7616.85, que
   no es múltiplo del tick 0.25 — el subgraph leído parece una línea calculada,
   no el extremo overnight. Pendiente de validar el Study ID del Input 15.
5. **`dva_rth.*` e `ib.*` valen 0 en premarket** — las clasificaciones derivadas
   ahora lo detectan y exportan `SIN_DATOS`.

Campos validados en vivo contra el chart: `fsvwap`, `desv1_*` (ETH y RTH),
`pva` completo, `ib` en horario RTH, `adr` (high/low), `delta`.
Pendientes de RE-validar en vivo tras los arreglos: `niveles` (pHOD/pLOD/
pClose/day_open con el mapeo corregido), `adr.pct_completado`, clasificaciones.

## Calibración imbalanceado/rotacional (ojo de donAdri, 2026-07-13)
- Lun 13-jul ~16:00 ES (VWAP +0.47× la 1ª desv en 3h, precio cruzando ambos lados) → **ROTACIONAL**
- Vie 10-jul ~21:00 ES (precio cabalgando +1σ/+1.8σ sin re-entrar) → **IMBALANCEADO**
- Mar 7-jul ~20:00 ES (VWAP −0.27×σ/1.5h, precio cruzando) → **ROTACIONAL**

Conclusión para el etiquetador: **la pendiente sola no basta** — criterio doble:
pendiente ≥ ~0.6-0.7× la 1ª desv en 3h **O** precio persistiendo más allá de la
1ª desviación sin re-entrar al valor. Si el precio cruza el VWAP por ambos lados,
es rotacional aunque haya deriva.
