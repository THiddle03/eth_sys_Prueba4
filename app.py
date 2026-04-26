import streamlit as st
import biosteam as bst
import thermosteam as tmo
import pandas as pd
import google.generativeai as genai
import os
import uuid
import streamlit.components.v1 as components


# 1. CONFIGURACIÓN DE PÁGINA
st.set_page_config(page_title="Simulador Bioetanol Pro v5", layout="wide")

# 2. FUNCIÓN DE SIMULACIÓN
def correr_simulacion(flow_water, flow_eth, temp_mosto, T_flash, P_flash, 
                      precio_elec, precio_vapor, precio_agua, precio_mp, precio_etanol):
    
    bst.main_flowsheet.clear()
    chemicals = tmo.Chemicals(["Water", "Ethanol"])
    bst.settings.set_thermo(chemicals)

    # Configuración de precios dinámicos
    bst.PowerUtility.price = precio_elec
    vapor = bst.HeatUtility.get_agent("low_pressure_steam")
    vapor.heat_transfer_price = precio_vapor
    agua = bst.HeatUtility.get_agent("cooling_water")
    agua.heat_transfer_price = precio_agua

    # --- CORRIENTES ---
    mosto = bst.Stream("1_MOSTO", Water=flow_water, Ethanol=flow_eth, units="kg/hr",
                       T=temp_mosto + 273.15, P=101325)
    mosto.price = precio_mp
    vinazas_retorno = bst.Stream("Vinazas_Retorno", T=95+273.15, P=3*101325)

    # --- EQUIPOS ---
    P110 = bst.Pump("P110", ins=mosto, P=4*101325)
    W210 = bst.HXprocess("W210", ins=(P110-0, vinazas_retorno), outs=("3_Mosto_Pre", "Drenaje"), phase0="l", phase1="l")
    W210.outs[0].T = 85 + 273.15
    W310 = bst.HXutility("W310", ins=W210-0, outs="Mezcla", T=T_flash+273.15)
    V411 = bst.IsenthalpicValve("V411", ins=W310-0, outs="Mezcla_Bifasica", P=P_flash*101325)
    K410 = bst.Flash("K410", ins=V411-0, outs=("Vapor_caliente", "Vinazas"), P=P_flash*101325, Q=0)
    W510 = bst.HXutility("W510", ins=K410-0, outs="Producto_Final", T=25+273.15)
    
    producto = W510.outs[0]
    producto.price = precio_etanol
    P510 = bst.Pump("P510", ins=K410-1, outs=vinazas_retorno, P=3*101325)

    # --- SISTEMA ---
    eth_sys = bst.System("planta_etanol", path=(P110, W210, W310, V411, K410, W510, P510))
    
    try:
        eth_sys.simulate()
    except Exception as e:
        return None, None, None, None, f"Error: {e}"

    # --- REPORTE MATERIA ---
    datos_mat = []
    for s in eth_sys.streams:
        if s.F_mass > 0.01:
            datos_mat.append({
                "Corriente": s.ID,
                "Temp (°C)": round(s.T - 273.15, 2),
                "Presión (bar)": round(s.P / 100000, 3),
                "Flujo (kg/h)": round(s.F_mass, 2),
                "% Etanol": f"{(s.imass['Ethanol']/s.F_mass if s.F_mass > 0 else 0):.1%}",
                "% Agua": f"{(s.imass['Water']/s.F_mass if s.F_mass > 0 else 0):.1%}"
            })
    
    # --- REPORTE ENERGÍA ---
  # --- REPORTE ENERGÍA (CORREGIDO) ---
    datos_en = []
    for u in eth_sys.units:
        # 1. Calculamos calor de servicios auxiliares (W310, W510, etc.)
        calor_util = sum([hu.duty for hu in u.heat_utilities])/3600 if hasattr(u, "heat_utilities") else 0
        
        # 2. Caso especial para W210 (HXprocess): extraer carga térmica de proceso
        if u.ID == "W210" and hasattr(u, 'H'):
            # El duty en HXprocess se puede obtener por la diferencia de entalpía o el atributo .duty
            calor_proceso = u.duty / 3600 # Convertir de kJ/h a kW (kJ/s)
            calor = calor_proceso
        else:
            calor = calor_util

        potencia = u.power_utility.rate if u.power_utility else 0
        
        # Filtro para mostrar solo equipos con consumo/transferencia significativa
        if abs(calor) > 0.001 or potencia > 0.001:
            datos_en.append({
                "Equipo": u.ID, 
                "Calor (kW)": round(calor, 2), 
                "Potencia (kW)": round(potencia, 2)
            })
    
    df_en = pd.DataFrame(datos_en)

# --- TEA ROBUSTO (ACTUALIZADO) ---
    class TEA_Robusto(bst.TEA):
        def _DPI(self, installed_equipment_cost): return self.purchase_cost
        def _TDC(self, DPI): return DPI
        def _FCI(self, TDC): return self.purchase_cost * self.lang_factor
        def _TCI(self, FCI): return FCI + self.WC
        def _FOC(self, FCI): return 0.0
        @property
        def VOC(self): return self.system.material_cost + self.system.utility_cost

    tea = TEA_Robusto(
        system=eth_sys, IRR=0.15, duration=(2025, 2045), depreciation='MACRS7',
        income_tax=0.3, operating_days=330, lang_factor=4.0, construction_schedule=(0.4, 0.6),
        WC_over_FCI=0.05, startup_months=6, startup_FOCfrac=0.5, startup_VOCfrac=0.5,
        startup_salesfrac=0.5, finance_interest=0, finance_years=0, finance_fraction=0
    )
    
    tea.IRR = 0.0
    costo_p = tea.solve_price(producto)
    
    # Diccionario con los nuevos parámetros solicitados
    ind_econ = {
        "Costo Producción ($/kg)": round(costo_p, 3),
        "Precio Venta ($/kg)": round(precio_etanol, 3), # Parámetro agregado
        "NPV (MUSD)": round(tea.NPV/1e6, 2),
        "ROI (%)": round(tea.ROI*100, 1),
        "PBP (Años)": round(tea.PBP, 2) # Parámetro agregado
    }

    p_path = f"pfd_{uuid.uuid4().hex[:8]}.png"
    try:
        eth_sys.diagram(file=p_path.replace(".png", ""), format="png", display=False)
    except:
        p_path = None

    return pd.DataFrame(datos_mat), pd.DataFrame(datos_en), ind_econ, p_path, None

# 3. INTERFAZ DE USUARIO
st.title("🧪 Simulador Bioetanol: Control Termodinámico y Económico")

# BARRA LATERAL
st.sidebar.header("🌡️ Parámetros Proceso")
f_w = st.sidebar.slider("Agua (kg/h)", 100, 3000, 900)
f_e = st.sidebar.slider("Etanol (kg/h)", 50, 2000, 100)
t_mosto = st.sidebar.slider("Temp. Alimentación Mosto (°C)", 10, 50, 25)
t_flash = st.sidebar.slider("Temp. Salida W310 (°C)", 70, 500, 92)
p_flash = st.sidebar.slider("Presión Separador K410 (atm)", 0.1, 15.0, 1.0, step=0.1)

st.sidebar.divider()
st.sidebar.header("💰 Parámetros Económicos")
# Nuevos Sliders Solicitados
p_elec = st.sidebar.slider("Precio Electricidad ($/kWh)", 0.01, 0.25, 0.085, step=0.005)
p_agua_c = st.sidebar.slider("Precio Agua Enfr. ($/MJ)", 0.0001, 0.01, 0.0005, step=0.0001, format="%.4f")
# Sliders mantenidos
p_vapor = st.sidebar.slider("Precio Vapor ($/MJ)", 0.01, 0.10, 0.025, step=0.005)
p_mp = st.sidebar.slider("Precio Materia Prima ($/kg)", 0.01, 0.50, 0.05, step=0.01)
p_etanol = st.sidebar.slider("Precio Venta Etanol ($/kg)", 0.5, 25.0, 1.2, step=0.1)

# Lógica de Simulación
if st.sidebar.button("Simular Proceso", type="primary"):
    dm, de, ec, pf, err = correr_simulacion(f_w, f_e, t_mosto, t_flash, p_flash, 
                                            p_elec, p_vapor, p_agua_c, p_mp, p_etanol)
    if err:
        st.error(err)
    else:
        st.session_state['resultados'] = (dm, de, ec, pf)

# ... (Todo el código anterior de simulación y lógica se mantiene igual)

# MOSTRAR RESULTADOS
if 'resultados' in st.session_state:
    dm, de, ec, pf = st.session_state['resultados']
    
    if pf and os.path.exists(pf):
        st.image(pf, caption="PFD dinámico generado por la simulación")

    

    st.divider()
    col1, col2 = st.columns(2)
    with col1:
        st.subheader("📊 Balances de Materia")
        st.dataframe(dm, use_container_width=True)
        st.subheader("📈 Economía")
        st.table(pd.DataFrame(list(ec.items()), columns=["Indicador", "Valor"]))
        
    with col2:
        st.subheader("⚡ Energía")
        st.dataframe(de, use_container_width=True)
        
        # --- TUTOR IA INTERACTIVO ---
        st.divider()
        st.subheader("🤖 Tutor IA Interactivo")
        # ... (Resto del código del Tutor IA)
        # --- TUTOR IA INTERACTIVO (Mismo código anterior) ---
        
        api_key = st.secrets.get("GEMINI_API_KEY")
        if api_key:
            user_question = st.text_input("Hazle una pregunta al tutor sobre los resultados:")
            
            if st.button("Enviar al Tutor"):
                if user_question:
                    with st.spinner('Analizando...'):
                        genai.configure(api_key=api_key)
                        model = genai.GenerativeModel('gemini-2.5-pro')
                        contexto = f"""
                        Eres un experto en ingeniería química.
                        Resultados: {dm.to_string()}
                        Economía: {ec}
                        Precios: Elec={p_elec}$, Agua={p_agua_c}$, Vapor={p_vapor}$, MP={p_mp}$.
                        Condiciones: Temp={t_flash}C, Pres={p_flash}atm.
                        Responde en <250 palabras de forma didáctica.
                        """
                        full_prompt = f"{contexto}\n\nPregunta: {user_question}"
                        try:
                            response = model.generate_content(full_prompt)
                            st.info(response.text)
                        except Exception as e:
                            st.error(f"Error: {e}")
                else:
                    st.warning("Escribe una pregunta.")
        else:
            st.warning("Falta GEMINI_API_KEY.")
    # -------------------------------------------------------------

#DFP en SVG para ver los resultados de forma interactiva

# 1. MAPEO DE COORDENADAS (AJUSTE FINO)
# Basado en las dimensiones del nuevo archivo D_eth_sys.svg
# Formato: [X, Y, Ancho, Alto]
ZONAS_EQUIPOS = {
    "P-110": [160, 5, 55, 55],
    "W-210": [365, 85, 75, 75],
    "W-310": [500, 280, 70, 70],
    "V-411": [620, 370, 60, 45],
    "K-410": [800, 300, 95, 180],
    "W-510": [935, 450, 80, 90],
    "P-510": [1010, 650, 60, 65]
}

def generar_pfd_interactivo(datos_simulacion):
    # Cargar el contenido del archivo SVG
    ruta_svg = "D_eth_sys.svg"
    if not os.path.exists(ruta_svg):
        return "Error: No se encontró el archivo D_eth_sys.svg en el directorio."
    
    with open(ruta_svg, "r", encoding="utf-8") as f:
        svg_content = f.read()

    # Generar la capa de interacción (hotspots)
    capa_interactiva = ""
    for equipo, pos in ZONAS_EQUIPOS.items():
        # Limpiar el nombre para buscar en el diccionario de datos (P-110 -> P110)
        id_sim = equipo.replace("-", "")
        info = datos_simulacion.get(id_sim, {"Estado": "Simulando..."})
        
        # Formatear el contenido del tooltip (lo que verá el usuario)
        tooltip_html = f"<b>{equipo}</b><br>"
        for clave, valor in info.items():
            tooltip_html += f"{clave}: {valor}<br>"
        
        # Crear rectángulos de detección
        capa_interactiva += f"""
        <rect x="{pos[0]}" y="{pos[1]}" width="{pos[2]}" height="{pos[3]}" 
              fill="white" fill-opacity="0" style="cursor:pointer;"
              onmouseover="showTip(event, '{tooltip_html}')" 
              onmouseout="hideTip()"
              onclick="alert('{equipo}\\n{'-'*15}\\n' + '{tooltip_html}'.replace(/<br>/g, '\\n').replace(/<b>/g, '').replace(/<\\/b>/g, ''))"/>
        """

    # HTML completo con lógica de Tooltips y el SVG embebido
    return f"""
    <div id="wrapper" style="position: relative; display: inline-block; width: 100%;">
        <div id="tooltip-box" style="position: fixed; display: none; background: rgba(20, 20, 20, 0.9); 
             color: #00ffcc; padding: 12px; border-radius: 8px; font-family: 'Segoe UI', Tahoma; 
             font-size: 13px; z-index: 10000; pointer-events: none; border: 1px solid #00ffcc;
             box-shadow: 0px 0px 15px rgba(0,255,204,0.3);"></div>
        {svg_content.replace('</svg>', capa_interactiva + '</svg>')}
    </div>
    <script>
        const tipBox = document.getElementById('tooltip-box');
        function showTip(e, text) {{
            tipBox.innerHTML = text;
            tipBox.style.display = 'block';
            moverTip(e);
        }}
        function hideTip() {{ tipBox.style.display = 'none'; }}
        function moverTip(e) {{
            tipBox.style.left = (e.clientX + 20) + 'px';
            tipBox.style.top = (e.clientY + 20) + 'px';
        }}
        document.addEventListener('mousemove', moverTip);
    </script>
    """

# 2. INTEGRACIÓN CON LOS RESULTADOS DE BIOSTEAM
if 'resultados' in st.session_state:
    dm, de, ec, pf = st.session_state['resultados']
    
    # Extraer valores reales de la simulación actual
    # Se usan valores por defecto del DFP [cite: 9, 23, 32, 48] si la unidad no está en la tabla
    # Dentro de: if 'resultados' in st.session_state:
    datos_actualizados = {
        "P110": {"Potencia": f"{de[de['Equipo']=='P110']['Potencia (kW)'].values[0] if 'P110' in de['Equipo'].values else '0.0'} kW"},
        "W210": {"Carga Térmica": f"{de[de['Equipo']=='W210']['Calor (kW)'].values[0] if 'W210' in de['Equipo'].values else 'Recuperación'} kW"},
        "W310": {"Calor (Vapor)": f"{de[de['Equipo']=='W310']['Calor (kW)'].values[0] if 'W310' in de['Equipo'].values else '0.0'} kW"},
        "V411": {"Presión": f"{dm[dm['Corriente']=='Mezcla_Bifasica']['Presión (bar)'].values[0] if 'Mezcla_Bifasica' in dm['Corriente'].values else '1.0'} bar"},
         "K410": {
            "Temp": f"{dm[dm['Corriente']=='Vapor_caliente']['Temp (°C)'].values[0] if 'Vapor_caliente' in dm['Corriente'].values else '92.17'} °C",
            "Presión": f"{dm[dm['Corriente']=='Vapor_caliente']['Presión (bar)'].values[0] if 'Vapor_caliente' in dm['Corriente'].values else '1.00'} bar"
        },
        "W510": {"Calor (Enf.)": f"{de[de['Equipo']=='W510']['Calor (kW)'].values[0] if 'W510' in de['Equipo'].values else '0.0'} kW"},
        "P510": {"Potencia": f"{de[de['Equipo']=='P510']['Potencia (kW)'].values[0] if 'P510' in de['Equipo'].values else '0.0'} kW"}
    }




    
    st.divider()
    st.subheader("🧪 Gemelo Digital: Monitoreo en Tiempo Real")
    st.info("Pasa el mouse sobre el diagrama para auditar los resultados dinámicos.")
    
    # Renderizar el componente
    html_interactivo = generar_pfd_interactivo(datos_actualizados)
    components.html(html_interactivo, height=750, scrolling=True)
