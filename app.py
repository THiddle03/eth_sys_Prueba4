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

def preparar_datos_interactivos(dm, de):
    # Función auxiliar para evitar errores de búsqueda
    def buscar(df, col, id, valor):
        try:
            return df[df[col] == id][valor].values[0]
        except:
            return 0

    return {
        "P110": {"pot": buscar(de, 'Equipo', 'P110', 'Potencia (kW)')},
        "W210": {"calor": buscar(de, 'Equipo', 'W210', 'Calor (kW)')},
        "W310": {"calor": buscar(de, 'Equipo', 'W310', 'Calor (kW)')},
        "V411": {"pres": buscar(dm, 'Corriente', 'Mezcla_Bifasica', 'Presión (bar)')},
        "K410": {
            "temp": buscar(dm, 'Corriente', 'Vapor_caliente', 'Temp (°C)'), # 92.17°C según diseño [cite: 25]
            "pres": buscar(dm, 'Corriente', 'Vapor_caliente', 'Presión (bar)'), # 1 bar según diseño [cite: 27]
            "calor": buscar(de, 'Equipo', 'K410', 'Calor (kW)')
        },
        "P510": {"pot": buscar(de, 'Equipo', 'P510', 'Potencia (kW)')}
    }

def renderizar_svg_personalizado(datos):
    # Cargamos el contenido de tu SVG (debes asegurarte que el archivo esté en la misma carpeta)
    with open("DFP_eth_sys.svg", "r") as f:
        svg_content = f.read()

    # Definimos los Tooltips dinámicos
    tooltips_js = f"""
        const data = {{
            "P-110": "Bomba Centrífuga<br>Potencia: {datos['P110']['pot']} kW",
            "W-210": "Intercambiador Coraza y Tubos<br>Carga: {datos['W210']['calor']} kW",
            "W-310": "Calentador de Mezcla<br>Carga: {datos['W310']['calor']} kW",
            "V-411": "Válvula de Expansión<br>P. Salida: {datos['V411']['pres']} bar",
            "K-410": "Tanque Flash<br>Temp: {datos['K410']['temp']} °C<br>P: {datos['K410']['pres']} bar",
            "P-510": "Bomba de Fondos<br>Potencia: {datos['P510']['pot']} kW"
        }};
    """

    return f"""
    <html>
    <head>
        <style>
            .tooltip {{
                position: absolute; display: none; padding: 10px;
                background: rgba(0, 0, 0, 0.85); color: white;
                border-radius: 4px; font-family: sans-serif; font-size: 12px;
                z-index: 1000; pointer-events: none;
            }}
            /* Estilo para resaltar los equipos al pasar el mouse */
            svg [id] {{ cursor: pointer; transition: opacity 0.2s; }}
            svg [id]:hover {{ opacity: 0.7; filter: brightness(1.2); }}
        </style>
    </head>
    <body>
        <div id="tooltip" class="tooltip"></div>
        <div id="svg-container">{svg_content}</div>
        
        <script>
            {tooltips_js}
            const tooltip = document.getElementById('tooltip');
            const svg = document.querySelector('svg');

            // Detectar mouse sobre cualquier elemento con un ID que coincida con nuestra data
            svg.addEventListener('mouseover', (e) => {{
                const targetId = e.target.closest('[id]')?.id;
                if (data[targetId]) {{
                    tooltip.innerHTML = `<strong>${{targetId}}</strong><br>${{data[targetId]}}`;
                    tooltip.style.display = 'block';
                }}
            }});

            svg.addEventListener('mousemove', (e) => {{
                tooltip.style.left = (e.pageX + 15) + 'px';
                tooltip.style.top = (e.pageY - 15) + 'px';
            }});

            svg.addEventListener('mouseout', () => {{
                tooltip.style.display = 'none';
            }});
        </script>
    </body>
    </html>
    """
if 'resultados' in st.session_state:
    dm, de, ec, pf = st.session_state['resultados']
    
    st.divider()
    st.subheader("🗺️ Diagrama de Proceso (SVG Personalizado)")
    
    # Procesar los datos de BioSTEAM
    datos_ui = preparar_datos_interactivos(dm, de)
    
    # Generar y renderizar el HTML con el SVG interactivo
    html_interactivo = renderizar_svg_personalizado(datos_ui)
    components.html(html_interactivo, height=600, scrolling=True)
