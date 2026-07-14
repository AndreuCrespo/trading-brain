// MCPDataExport.cpp
// Sierra Chart ACSIL Study — exporta todos los valores del gráfico a JSON
//
// INSTALACIÓN:
//   1. Copia este archivo a: C:\SierraChart\ACS_Source\MCPDataExport.cpp
//   2. En Sierra Chart: Analysis > Build Custom Studies DLL
//   3. Añade el estudio "MCP Data Export" a tu gráfico principal (región 0)
//   4. El archivo sc_data.json se actualiza en cada tick

#include "sierrachart.h"
#include <fstream>
#include <sstream>
#include <iomanip>
#include <locale>
#include <string>

SCDLLName("MCPDataExport")

SCSFExport scsf_MCPDataExport(SCStudyInterfaceRef sc)
{
    // ----------------------------------------------------------------
    // CONFIGURACIÓN
    // ----------------------------------------------------------------
    if (sc.SetDefaults)
    {
        sc.GraphName       = "MCP Data Export";
        sc.StudyVersion    = 1;
        sc.AutoLoop        = 0;
        sc.UpdateAlways    = 1;
        sc.GraphRegion     = 0;
        sc.DrawStudyUnderneathMainPriceGraph = 1;

        // Ruta del archivo de salida
        sc.Input[0].Name = "Ruta archivo JSON";
        sc.Input[0].SetString("C:\\Users\\Usuario\\Desktop\\Trading bot\\sc_data.json");

        // IDs de estudios — coinciden con los IDs de tu gráfico
        sc.Input[1].Name  = "ID: DVA ETH (ECIVwapV1.5)";        sc.Input[1].SetInt(15);
        sc.Input[2].Name  = "ID: rthVWAP";                       sc.Input[2].SetInt(23);
        sc.Input[3].Name  = "ID: DVARTH 2da desv";               sc.Input[3].SetInt(26);
        sc.Input[4].Name  = "ID: DVARTH 3ra desv";               sc.Input[4].SetInt(14);
        sc.Input[5].Name  = "ID: DVA2ETH (ref)";                 sc.Input[5].SetInt(10);
        sc.Input[6].Name  = "ID: DVA3ETH (ref)";                 sc.Input[6].SetInt(4);
        sc.Input[7].Name  = "ID: pVA";                           sc.Input[7].SetInt(5);
        sc.Input[8].Name  = "ID: IB High/Low";                   sc.Input[8].SetInt(21);
        sc.Input[9].Name  = "ID: ADR Bands";                     sc.Input[9].SetInt(25);
        sc.Input[10].Name = "ID: Delta Cumulative";              sc.Input[10].SetInt(18);
        sc.Input[11].Name = "ID: RTH Vol Profile";               sc.Input[11].SetInt(6);
        sc.Input[12].Name = "ID: pHOD-LOD-pCl-Open";            sc.Input[12].SetInt(9);
        sc.Input[13].Name = "ID: wVWAP";                         sc.Input[13].SetInt(13);
        sc.Input[14].Name = "ID: mVWAP";                         sc.Input[14].SetInt(24);
        sc.Input[15].Name = "ID: ONH-ONL";                       sc.Input[15].SetInt(12);
        sc.Input[16].Name = "ID: Collisions (puntos)";           sc.Input[16].SetInt(22);

        // Mínimo de segundos entre escrituras del JSON (0 = cada tick)
        sc.Input[17].Name = "Throttle escritura (segundos)";     sc.Input[17].SetInt(1);

        return;
    }

    // Solo procesa en la última barra (tick más reciente)
    if (sc.Index != sc.ArraySize - 1)
        return;

    // Throttle: no reescribir el fichero más de una vez por N segundos.
    // Escribir en cada tick castiga el hilo del chart y multiplica la ventana
    // de lectura parcial para cualquier consumidor externo del JSON.
    {
        int throttle_sec = sc.Input[17].GetInt();
        if (throttle_sec > 0)
        {
            double now_days  = sc.CurrentSystemDateTime.GetAsDouble();
            double last_days = sc.GetPersistentDouble(1);
            if (last_days > 0 && (now_days - last_days) * 86400.0 < (double)throttle_sec)
                return;
            sc.SetPersistentDouble(1, now_days);
        }
    }

    int CN = sc.ChartNumber;

    // ----------------------------------------------------------------
    // HELPER: leer valor de un subgraph de otro estudio
    // ----------------------------------------------------------------
    auto Get = [&](int studyID, int subgraph) -> double {
        SCFloatArray arr;
        sc.GetStudyArrayFromChartUsingID(CN, studyID, subgraph, arr);
        int sz = arr.GetArraySize();
        if (sz == 0) return 0.0;
        return (double)arr[sz - 1];
    };

    // ----------------------------------------------------------------
    // LEER VALORES
    // ----------------------------------------------------------------

    // --- FSVWAP + DVA ETH (ID:15) ---
    // Subgraphs típicos ECIVwapV1.5: 0=VWAP, 1=+1D, 2=-1D, 3=+2D, 4=-2D, 5=+3D, 6=-3D
    double fsvwap    = Get(sc.Input[1].GetInt(), 0);
    double eth_1up   = Get(sc.Input[1].GetInt(), 1);
    double eth_1dn   = Get(sc.Input[1].GetInt(), 2);
    // Los Study Subgraphs Reference exponen PARES POR LADO, no por nivel:
    // ID DVA2ETH (Input 5) = par INFERIOR (SG0=-2ª desv, SG1=-3ª desv)
    // ID DVA3ETH (Input 6) = par SUPERIOR (SG0=+2ª desv, SG1=+3ª desv)
    // Validado en vivo 2026-07-14 contra el chart de trigger (ES).
    double eth_2up   = Get(sc.Input[6].GetInt(), 0);  // +2ª (par superior SG0)
    double eth_2dn   = Get(sc.Input[5].GetInt(), 0);  // -2ª (par inferior SG0)
    double eth_3up   = Get(sc.Input[6].GetInt(), 1);  // +3ª (par superior SG1)
    double eth_3dn   = Get(sc.Input[5].GetInt(), 1);  // -3ª (par inferior SG1)

    // --- RTH VWAP + DVA RTH (ID:23) ---
    double rthvwap   = Get(sc.Input[2].GetInt(), 0);
    double rth_1up   = Get(sc.Input[2].GetInt(), 1);
    double rth_1dn   = Get(sc.Input[2].GetInt(), 2);
    // Mismo patrón que en ETH — los dos DVARTH son pares POR LADO:
    // ID:26 (Input 3) = par INFERIOR (SG0=-2ª, SG1=-3ª)
    // ID:14 (Input 4) = par SUPERIOR (SG0=+2ª, SG1=+3ª)
    // Confirmado con los valores del sample del 2026-07-02 (vwap 7549.59:
    // ID:26 → 7524/7498 debajo, ID:14 → 7575/7600 encima).
    double rth_2up   = Get(sc.Input[4].GetInt(), 0);  // +2ª (par superior SG0)
    double rth_2dn   = Get(sc.Input[3].GetInt(), 0);  // -2ª (par inferior SG0)
    double rth_3up   = Get(sc.Input[4].GetInt(), 1);  // +3ª (par superior SG1)
    double rth_3dn   = Get(sc.Input[3].GetInt(), 1);  // -3ª (par inferior SG1)

    // --- pVA (ID:5): orden Sierra Volume Value Area Lines = 0=POC, 1=VAH, 2=VAL ---
    double ppoc = Get(sc.Input[7].GetInt(), 0);
    double pvah = Get(sc.Input[7].GetInt(), 1);
    double pval = Get(sc.Input[7].GetInt(), 2);

    // --- RTH Vol Profile (ID:6): VA actual = 0=POC, 1=VAH, 2=VAL ---
    double cur_poc = Get(sc.Input[11].GetInt(), 0);
    double cur_vah = Get(sc.Input[11].GetInt(), 1);
    double cur_val = Get(sc.Input[11].GetInt(), 2);

    // --- IB (ID:21): 0=IBH, 1=IBL ---
    double ibh = Get(sc.Input[8].GetInt(), 0);
    double ibl = Get(sc.Input[8].GetInt(), 1);

    // --- ADR (ID:25): 0=ADR High, 1=ADR Low ---
    double adr_high = Get(sc.Input[9].GetInt(), 0);
    double adr_low  = Get(sc.Input[9].GetInt(), 1);

    // --- Delta Cumulative Bars (ID:18): 0=Open, 1=High, 2=Low, 3=Close ---
    double delta_open  = Get(sc.Input[10].GetInt(), 0);
    double delta_high  = Get(sc.Input[10].GetInt(), 1);
    double delta_low   = Get(sc.Input[10].GetInt(), 2);
    double delta_close = Get(sc.Input[10].GetInt(), 3);

    // --- Daily OHLC (ID:9): 0=Open(hoy), 1=High(prev), 2=Low(prev), 3=Close(prev) ---
    // CORREGIDO: el mapeo anterior (0=pHOD, 1=pLOD, 2=pClose, 3=Open) producía
    // prev_hod < prev_lod en el sample real. Los valores del sample encajan con
    // el orden estándar Open/High/Low/Close del estudio Daily OHLC.
    double day_open   = Get(sc.Input[12].GetInt(), 0);
    double prev_hod   = Get(sc.Input[12].GetInt(), 1);
    double prev_lod   = Get(sc.Input[12].GetInt(), 2);
    double prev_close = Get(sc.Input[12].GetInt(), 3);

    // --- wVWAP (ID:13), mVWAP (ID:24) ---
    double wvwap = Get(sc.Input[13].GetInt(), 0);
    double mvwap = Get(sc.Input[14].GetInt(), 0);

    // --- ONH / ONL (ID:12) ---
    double onh = Get(sc.Input[15].GetInt(), 0);
    double onl = Get(sc.Input[15].GetInt(), 1);

    // --- Precio actual de la barra ---
    int    last   = sc.ArraySize - 1;
    double price  = (double)sc.Close[last];
    double high   = (double)sc.High[last];
    double low    = (double)sc.Low[last];
    double volume = (double)sc.Volume[last];

    // Volumen relativo: últimas 10 barras CERRADAS (la última está en formación
    // y compararla con barras completas infravalora sistemáticamente el RVOL;
    // en charts de barras de volumen este campo mide "fracción de barra
    // completada", no RVOL — ver README).
    double vol_sum = 0.0;
    int    vol_cnt = 0;
    for (int i = last - 10; i <= last - 1; i++)
    {
        if (i >= 0) { vol_sum += sc.Volume[i]; vol_cnt++; }
    }
    double avg_vol  = (vol_cnt > 0) ? (vol_sum / vol_cnt) : 0.0;
    double rel_vol  = (avg_vol > 0) ? (volume / avg_vol) : 0.0;

    // Rango real de la SESIÓN actual (no de la última barra): recorre hacia
    // atrás las barras del mismo trading day acumulando high/low.
    double sess_high = high;
    double sess_low  = low;
    {
        int trading_day = sc.GetTradingDayDate(sc.BaseDateTimeIn[last]);
        for (int i = last - 1; i >= 0; i--)
        {
            if (sc.GetTradingDayDate(sc.BaseDateTimeIn[i]) != trading_day)
                break;
            if (sc.High[i] > sess_high) sess_high = (double)sc.High[i];
            if (sc.Low[i]  < sess_low)  sess_low  = (double)sc.Low[i];
        }
    }

    // ADR completado % — sobre el rango de sesión real. Si el estudio ADR no
    // da bandas válidas, se exporta 0 en vez de un porcentaje inventado.
    double adr_range = (adr_high > adr_low) ? (adr_high - adr_low) : 0.0;
    double sess_range = sess_high - sess_low;
    double adr_pct    = (adr_range > 0.0) ? (sess_range / adr_range) * 100.0 : 0.0;

    // ----------------------------------------------------------------
    // CLASIFICACIONES (lógica del playbook)
    // ----------------------------------------------------------------

    // GUARDA: un estudio ausente/no calculado devuelve 0.0 en Get(). Sin esta
    // comprobación, en premarket (IB y DVA RTH a 0) las clasificaciones
    // afirmaban "ROTO_ARRIBA"/"IMBALANCEADO" contra niveles inexistentes.
    // Con datos inválidos se exporta "SIN_DATOS" en lugar de una etiqueta falsa.
    auto Valid = [](double v) { return v > 0.0; };

    // Condición DVA (posición instantánea del precio vs 1ª desviación).
    // OJO: esto NO es el criterio calibrado de imbalance del playbook
    // (pendiente sostenida + persistencia fuera de la 1ª desv — ver README);
    // es solo dónde está el precio AHORA respecto a las bandas.
    const char* dva_eth_cond = "SIN_DATOS";
    if (Valid(eth_1up) && Valid(eth_1dn))
        dva_eth_cond = (price > eth_1up || price < eth_1dn) ? "FUERA_DESV1" : "DENTRO_DESV1";

    const char* dva_rth_cond = "SIN_DATOS";
    if (Valid(rth_1up) && Valid(rth_1dn))
        dva_rth_cond = (price > rth_1up || price < rth_1dn) ? "FUERA_DESV1" : "DENTRO_DESV1";

    // Localización precio vs VWAPs
    const char* vs_fsvwap  = Valid(fsvwap)  ? ((price > fsvwap)  ? "ENCIMA" : "DEBAJO") : "SIN_DATOS";
    const char* vs_rthvwap = Valid(rthvwap) ? ((price > rthvwap) ? "ENCIMA" : "DEBAJO") : "SIN_DATOS";
    const char* vs_wvwap   = Valid(wvwap)   ? ((price > wvwap)   ? "ENCIMA" : "DEBAJO") : "SIN_DATOS";
    const char* vs_mvwap   = Valid(mvwap)   ? ((price > mvwap)   ? "ENCIMA" : "DEBAJO") : "SIN_DATOS";

    // Estado IB (solo con IB formado y coherente)
    const char* ib_status = "SIN_DATOS";
    if (Valid(ibh) && Valid(ibl) && ibh > ibl)
    {
        ib_status = "DENTRO";
        if (price > ibh) ib_status = "ROTO_ARRIBA";
        else if (price < ibl) ib_status = "ROTO_ABAJO";
    }

    // Estado delta (barra actual)
    const char* delta_dir = (delta_close >= delta_open) ? "ALCISTA" : "BAJISTA";
    bool delta_neutral = (delta_close - delta_open) == 0.0;
    if (delta_neutral) delta_dir = "PLANO";

    // Precio vs pVA (exige VAH > VAL coherentes)
    const char* vs_pva = "SIN_DATOS";
    if (Valid(pvah) && Valid(pval) && pvah > pval)
    {
        vs_pva = "DENTRO_PVA";
        if (price > pvah)      vs_pva = "ENCIMA_PVA";
        else if (price < pval) vs_pva = "DEBAJO_PVA";
    }

    // Precio vs VA actual RTH — el estudio de origen está documentado como no
    // fiable (POC basura, VAH/VAL a veces invertidos): si llega incoherente,
    // NO se emite una etiqueta con aspecto limpio.
    const char* vs_curva = "NO_FIABLE";
    if (Valid(cur_vah) && Valid(cur_val) && cur_vah > cur_val)
    {
        vs_curva = "DENTRO_VA";
        if (price > cur_vah)      vs_curva = "ENCIMA_VA";
        else if (price < cur_val) vs_curva = "DEBAJO_VA";
    }

    // Timestamp
    SCDateTime dt = sc.BaseDateTimeIn[last];
    int Y, Mo, D, H, Mi, S;
    dt.GetDateTimeYMDHMS(Y, Mo, D, H, Mi, S);
    char ts[32];
    snprintf(ts, sizeof(ts), "%04d-%02d-%02dT%02d:%02d:%02d", Y, Mo, D, H, Mi, S);

    // ----------------------------------------------------------------
    // CONSTRUIR JSON
    // ----------------------------------------------------------------
    std::ostringstream j;
    // Locale clásico: si el proceso tuviera un locale global con coma decimal
    // (Windows en español), los números saldrían como 7568,25 = JSON inválido.
    j.imbue(std::locale::classic());
    j << std::fixed << std::setprecision(2);

    j << "{\n"
      << "  \"timestamp\": \"" << ts << "\",\n"
      << "  \"symbol\": \"" << sc.GetChartSymbol(CN).GetChars() << "\",\n"
      << "  \"price\": " << price << ",\n"
      << "  \"volume\": " << volume << ",\n"
      << "  \"relative_volume\": " << std::setprecision(2) << rel_vol << ",\n"

      << "  \"dva_eth\": {\n"
      << "    \"fsvwap\": " << fsvwap << ",\n"
      << "    \"condicion\": \"" << dva_eth_cond << "\",\n"
      << "    \"precio_vs_fsvwap\": \"" << vs_fsvwap << "\",\n"
      << "    \"desv1_arriba\": " << eth_1up << ",\n"
      << "    \"desv1_abajo\": " << eth_1dn << ",\n"
      << "    \"desv2_arriba\": " << eth_2up << ",\n"
      << "    \"desv2_abajo\": " << eth_2dn << ",\n"
      << "    \"desv3_arriba\": " << eth_3up << ",\n"
      << "    \"desv3_abajo\": " << eth_3dn << "\n"
      << "  },\n"

      << "  \"dva_rth\": {\n"
      << "    \"vwap\": " << rthvwap << ",\n"
      << "    \"condicion\": \"" << dva_rth_cond << "\",\n"
      << "    \"precio_vs_rthvwap\": \"" << vs_rthvwap << "\",\n"
      << "    \"desv1_arriba\": " << rth_1up << ",\n"
      << "    \"desv1_abajo\": " << rth_1dn << ",\n"
      << "    \"desv2_arriba\": " << rth_2up << ",\n"
      << "    \"desv2_abajo\": " << rth_2dn << ",\n"
      << "    \"desv3_arriba\": " << rth_3up << ",\n"
      << "    \"desv3_abajo\": " << rth_3dn << "\n"
      << "  },\n"

      << "  \"pva\": {\n"
      << "    \"vah\": " << pvah << ",\n"
      << "    \"val\": " << pval << ",\n"
      << "    \"poc\": " << ppoc << ",\n"
      << "    \"precio_vs_pva\": \"" << vs_pva << "\"\n"
      << "  },\n"

      << "  \"va_actual_rth\": {\n"
      << "    \"vah\": " << cur_vah << ",\n"
      << "    \"val\": " << cur_val << ",\n"
      << "    \"poc\": " << cur_poc << ",\n"
      << "    \"precio_vs_va\": \"" << vs_curva << "\"\n"
      << "  },\n"

      << "  \"ib\": {\n"
      << "    \"high\": " << ibh << ",\n"
      << "    \"low\": " << ibl << ",\n"
      << "    \"estado\": \"" << ib_status << "\",\n"
      << "    \"rango\": " << (ibh - ibl) << "\n"
      << "  },\n"

      << "  \"adr\": {\n"
      << "    \"high\": " << adr_high << ",\n"
      << "    \"low\": " << adr_low << ",\n"
      << "    \"rango\": " << adr_range << ",\n"
      << "    \"pct_completado\": " << adr_pct << "\n"
      << "  },\n"

      << "  \"delta\": {\n"
      << "    \"open\": " << delta_open << ",\n"
      << "    \"high\": " << delta_high << ",\n"
      << "    \"low\": " << delta_low << ",\n"
      << "    \"close\": " << delta_close << ",\n"
      << "    \"direccion\": \"" << delta_dir << "\"\n"
      << "  },\n"

      << "  \"niveles\": {\n"
      << "    \"wvwap\": " << wvwap << ",\n"
      << "    \"precio_vs_wvwap\": \"" << vs_wvwap << "\",\n"
      << "    \"mvwap\": " << mvwap << ",\n"
      << "    \"precio_vs_mvwap\": \"" << vs_mvwap << "\",\n"
      << "    \"onh\": " << onh << ",\n"
      << "    \"onl\": " << onl << ",\n"
      << "    \"prev_hod\": " << prev_hod << ",\n"
      << "    \"prev_lod\": " << prev_lod << ",\n"
      << "    \"prev_close\": " << prev_close << ",\n"
      << "    \"day_open\": " << day_open << "\n"
      << "  }\n"
      << "}\n";

    // ----------------------------------------------------------------
    // ESCRIBIR ARCHIVO — de forma atómica.
    // Antes se abría el destino truncándolo y se escribía in situ: un lector
    // externo que hiciera polling podía leer un JSON vacío o cortado. Ahora se
    // escribe a un .tmp y se renombra con MOVEFILE_REPLACE_EXISTING, que en el
    // mismo volumen es atómico: el lector siempre ve un JSON completo.
    // ----------------------------------------------------------------
    std::string out_path = sc.Input[0].GetString();
    std::string tmp_path = out_path + ".tmp";

    std::ofstream f(tmp_path.c_str(), std::ios::binary | std::ios::trunc);
    if (f.is_open())
    {
        f << j.str();
        f.close();
        if (!MoveFileExA(tmp_path.c_str(), out_path.c_str(),
                         MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH))
        {
            sc.AddMessageToLog("MCPDataExport: fallo al renombrar el JSON temporal", 1);
        }
    }
    else
    {
        sc.AddMessageToLog("MCPDataExport: no se pudo abrir el fichero de salida", 1);
    }
}
