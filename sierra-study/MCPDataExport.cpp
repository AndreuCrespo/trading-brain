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

        return;
    }

    // Solo procesa en la última barra (tick más reciente)
    if (sc.Index != sc.ArraySize - 1)
        return;

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
    double eth_2up   = Get(sc.Input[5].GetInt(), 0);  // DVA2ETH ref
    double eth_2dn   = Get(sc.Input[5].GetInt(), 1);
    double eth_3up   = Get(sc.Input[6].GetInt(), 0);  // DVA3ETH ref
    double eth_3dn   = Get(sc.Input[6].GetInt(), 1);

    // --- RTH VWAP + DVA RTH (ID:23) ---
    double rthvwap   = Get(sc.Input[2].GetInt(), 0);
    double rth_1up   = Get(sc.Input[2].GetInt(), 1);
    double rth_1dn   = Get(sc.Input[2].GetInt(), 2);
    double rth_2up   = Get(sc.Input[3].GetInt(), 0);  // DVARTH 2da
    double rth_2dn   = Get(sc.Input[3].GetInt(), 1);
    double rth_3up   = Get(sc.Input[4].GetInt(), 0);  // DVARTH 3ra
    double rth_3dn   = Get(sc.Input[4].GetInt(), 1);

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

    // --- Daily OHLC (ID:9): 0=pHOD, 1=pLOD, 2=pClose, 3=Open ---
    double prev_hod   = Get(sc.Input[12].GetInt(), 0);
    double prev_lod   = Get(sc.Input[12].GetInt(), 1);
    double prev_close = Get(sc.Input[12].GetInt(), 2);
    double day_open   = Get(sc.Input[12].GetInt(), 3);

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

    // Volumen de las últimas 10 barras para volumen relativo
    double vol_sum = 0.0;
    int    vol_cnt = 0;
    for (int i = last - 9; i <= last - 1; i++)
    {
        if (i >= 0) { vol_sum += sc.Volume[i]; vol_cnt++; }
    }
    double avg_vol  = (vol_cnt > 0) ? (vol_sum / vol_cnt) : 1.0;
    double rel_vol  = (avg_vol > 0) ? (volume / avg_vol) : 1.0;

    // ADR completado %
    double adr_range   = (adr_high > adr_low) ? (adr_high - adr_low) : 1.0;
    double sess_range  = high - low;  // simplificado — rango de sesión
    double adr_pct     = (sess_range / adr_range) * 100.0;

    // ----------------------------------------------------------------
    // CLASIFICACIONES (lógica del playbook)
    // ----------------------------------------------------------------

    // Condición DVA ETH
    const char* dva_eth_cond = "ROTACIONAL";
    if (price > eth_1up || price < eth_1dn) dva_eth_cond = "IMBALANCEADO";

    // Condición DVA RTH
    const char* dva_rth_cond = "ROTACIONAL";
    if (price > rth_1up || price < rth_1dn) dva_rth_cond = "IMBALANCEADO";

    // Localización precio vs FSVWAP
    const char* vs_fsvwap  = (price > fsvwap)  ? "ENCIMA" : "DEBAJO";
    const char* vs_rthvwap = (price > rthvwap) ? "ENCIMA" : "DEBAJO";
    const char* vs_wvwap   = (price > wvwap)   ? "ENCIMA" : "DEBAJO";
    const char* vs_mvwap   = (price > mvwap)   ? "ENCIMA" : "DEBAJO";

    // Estado IB
    const char* ib_status = "DENTRO";
    if (price > ibh) ib_status = "ROTO_ARRIBA";
    else if (price < ibl) ib_status = "ROTO_ABAJO";

    // Estado delta (barra actual)
    const char* delta_dir = (delta_close >= delta_open) ? "ALCISTA" : "BAJISTA";
    bool delta_neutral = (delta_close - delta_open) == 0.0;
    if (delta_neutral) delta_dir = "PLANO";

    // Precio vs pVA
    const char* vs_pva = "DENTRO_PVA";
    if (price > pvah)      vs_pva = "ENCIMA_PVA";
    else if (price < pval) vs_pva = "DEBAJO_PVA";

    // Precio vs VA actual RTH
    const char* vs_curva = "DENTRO_VA";
    if (price > cur_vah)      vs_curva = "ENCIMA_VA";
    else if (price < cur_val) vs_curva = "DEBAJO_VA";

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
    // ESCRIBIR ARCHIVO
    // ----------------------------------------------------------------
    std::ofstream f(sc.Input[0].GetString());
    if (f.is_open())
    {
        f << j.str();
        f.close();
    }
}
