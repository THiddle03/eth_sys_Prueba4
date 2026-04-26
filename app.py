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
    datos_en = []
    for u in eth_sys.units:
        calor = sum([hu.duty for hu in u.heat_utilities])/3600 if hasattr(u, "heat_utilities") else 0
        potencia = u.power_utility.rate if u.power_utility else 0
        if abs(calor) > 0.1 or potencia > 0.1:
            datos_en.append({"Equipo": u.ID, "Calor (kW)": round(calor, 2), "Potencia (kW)": round(potencia, 2)})

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

# Diccionario de zonas activas [x, y, ancho, alto]
EQUIPMENT_ZONES = {
    "P-110": [155, 10, 50, 50],   # Labeled as P-100 in your SVG
    "W-210": [340, 120, 65, 65],
    "W-310": [445, 340, 60, 60],
    "V-411": [560, 495, 55, 45],
    "K-410": [720, 385, 85, 215],
    "W-510": [845, 600, 70, 85],
    "P-510": [910, 820, 60, 60]   # Nota: Ajusta 'y' si P-510 queda fuera del visor
}


def generar_svg_interactivo_coords(datos_simulacion):
    # Cargar el contenido de tu archivo SVG
    ruta_svg = os.path.join(os.path.dirname(__file__), "DFP_eth_sys.svg")
    try:
        with open(ruta_svg, "r", encoding="utf-8") as f:
            svg_content = f.read()
    except FileNotFoundError:
        return "Error: No se encontró el archivo DFP_eth_sys.svg"

    # Generar los rectángulos de interacción (hotspots)
    hotspots = ""
    for equipo, coords in EQUIPMENT_ZONES.items():
        # Mapeo de nombres para extraer datos de la simulación
        id_bio = equipo.replace("-", "") # Convierte P-110 en P110 para el diccionario
        
        # Extraer datos dinámicos (usando los datos de dm y de que ya calculas)
        info = datos_simulacion.get(id_bio, {})
        texto_tooltip = f"<b>{equipo}</b><br>"
        for k, v in info.items():
            texto_tooltip += f"{k}: {v}<br>"

        hotspots += f"""
        <rect x="{coords[0]}" y="{coords[1]}" width="{coords[2]}" height="{coords[3]}" 
              fill="red" fill-opacity="0" style="cursor:pointer;"
              onmouseover="showTooltip(event, '{texto_tooltip}')" 
              onmouseout="hideTooltip()"/>
        """

    #HTML final que envuelve el SVG y añade el JavaScript de los Tooltips
    html_final = f"""
    <div id="container" style="position: relative; display: inline-block;">
        <div id="tooltip" style="position: fixed; display: none; background: rgba(0,0,0,0.85); 
             color: white; padding: 8px; border-radius: 4px; font-family: sans-serif; 
             font-size: 12px; z-index: 1000; pointer-events: none; border: 1px solid #555;"></div>
        {svg_content.replace('</svg>', hotspots + '</svg>')}
    </div>

    <script>
        const tip = document.getElementById('tooltip');
        function showTooltip(e, text) {{
            tip.innerHTML = text;
            tip.style.display = 'block';
            tip.style.left = (e.clientX + 15) + 'px';
            tip.style.top = (e.clientY - 15) + 'px';
        }}
        function hideTooltip() {{
            tip.style.display = 'none';
        }}
    </script>
    """
    return html_final
    
if 'resultados' in st.session_state:
    dm, de, ec, pf = st.session_state['resultados']

    # 1. Organizamos los datos para el diagrama basándonos en tus DataFrames
    datos_para_pfd = {
        "P110": {"Potencia": f"{de[de['Equipo']=='P110']['Potencia (kW)'].values[0]} kW"},
        #"W210": {"Calor": f"{de[de['Equipo']=='W210']['Calor (kW)'].values[0]} kW"},
        "W310": {"Calor": f"{de[de['Equipo']=='W310']['Calor (kW)'].values[0]} kW"},
        "V411": {"Presión Salida": f"{dm[dm['Corriente']=='Mezcla_Bifasica']['Presión (bar)'].values[0]} bar"},
        "K410": {
            "Temp": f"{dm[dm['Corriente']=='Vapor_caliente']['Temp (°C)'].values[0]} °C",
            "Presión": f"{dm[dm['Corriente']=='Vapor_caliente']['Presión (bar)'].values[0]} bar"
        },
        "W510": {"Calor": f"{de[de['Equipo']=='W510']['Calor (kW)'].values[0]} kW"},
        "P510": {"Potencia": f"{de[de['Equipo']=='P510']['Potencia (kW)'].values[0]} kW"}
    }

    # 2. Mostramos el diagrama
    st.divider()
    st.subheader("🗺️ Diagrama de Flujo Interactivo (Basado en Coordenadas)")
    html_interactivo = generar_svg_interactivo_coords(datos_para_pfd)
    components.html(html_interactivo, height=800, scrolling=True)
