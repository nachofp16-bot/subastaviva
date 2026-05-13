#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BOE Subastas Valencia
Busca pisos en subasta en la provincia de Valencia en el portal subastas.boe.es
y genera un informe HTML en el Escritorio.
"""

import json
import requests
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import os
import re
import subprocess
import sys
import time
import webbrowser

# ============================================================
# CONFIGURACION
# ============================================================
PROVINCIA_CODIGO = '46'       # Valencia
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CARPETA_INFORMES = os.path.join(SCRIPT_DIR, 'informes')
CARPETA_WEB = os.path.join(SCRIPT_DIR, 'web')
os.makedirs(CARPETA_INFORMES, exist_ok=True)
os.makedirs(CARPETA_WEB, exist_ok=True)
BASE_URL = 'https://subastas.boe.es'
SEARCH_URL = f'{BASE_URL}/subastas_ava.php'

# Palabras que identifican viviendas en la descripcion del bien
KEYWORDS_VIVIENDA = [
    'vivienda', 'piso', 'apartamento', 'atico', 'ático',
    'duplex', 'dúplex', 'estudio', 'planta', 'habitacion',
    'habitación', 'dormitorio', 'residencial'
]


# ============================================================
# BUSQUEDA
# ============================================================
def buscar_subastas(session):
    """Realiza la busqueda POST en el portal BOE y devuelve el HTML."""
    headers = {
        'User-Agent': (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36 (KHTML, like Gecko) '
            'Chrome/124.0.0.0 Safari/537.36'
        ),
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'es-ES,es;q=0.9',
        'Referer': SEARCH_URL,
        'Origin': BASE_URL,
    }

    # Parametros que funcionan: estado Celebrandose + tipo Inmueble + Valencia
    # IMPORTANTE: NO incluir sort_field (rompe la busqueda en el BOE)
    data = {
        'campo[0]': 'SUBASTA.ORIGEN',
        'dato[0]': '',
        'campo[2]': 'SUBASTA.ESTADO.CODIGO',
        'dato[2]': 'EJ',           # EJ = Celebrandose (activas ahora)
        'campo[3]': 'BIEN.TIPO',
        'dato[3]': 'I',            # I = Inmueble
        'campo[8]': 'BIEN.COD_PROVINCIA',
        'dato[8]': PROVINCIA_CODIGO,
        'page_hits': '500',
        'accion': 'Buscar',
    }

    try:
        resp = session.post(SEARCH_URL, data=data, headers=headers, timeout=30)
        resp.raise_for_status()
        resp.encoding = 'utf-8'
        return resp.text, None
    except requests.RequestException as e:
        return None, str(e)


# ============================================================
# EXTRACCION DE MUNICIPIO
# ============================================================
# Palabras que NO son municipios aunque aparezcan tras "en"
_FALSOS_POSITIVOS = {
    'la', 'el', 'los', 'las', 'un', 'una', 'este', 'esta', 'su', 'sus',
    'construccion', 'construcción', 'planta', 'calle', 'el edificio',
    'dicho', 'dicha', 'cuanto', 'virtud', 'propiedad', 'termino',
    'término', 'termino municipal', 'término municipal',
    'primera', 'segunda', 'tercera', 'cuarta', 'quinta', 'sexta',
    'septima', 'séptima', 'octava', 'novena', 'decima', 'décima',
    'baja', 'alta', 'bajo', 'alto',
}

def extraer_municipio(desc):
    """Extrae el nombre del municipio de una descripción de subasta."""
    if not desc:
        return ''

    # Clase de carácter amplia: letras normales + acentos + ñ + unicode replacements
    L = r'[A-Za-z\u00c0-\u024f\ufffd]'
    MUNI = L + r'(?:' + L + r'|\s|\-){1,35}'

    # 1. Formato AEAT: "46000 - MUNICIPIO (VALENCIA)"
    m = re.search(r'\d{5}\s*-\s*([^()\n,]{2,40})', desc)
    if m:
        return m.group(1).strip().title()

    # 2. "sita/situada/situado/radicado/ubicado en [la villa de] MUNICIPIO[,.]"
    m = re.search(
        r'(?:sita|situada|situado|radicad[ao]|enclavad[ao]|ubicad[ao])\s+en\s+'
        r'(?:la\s+villa\s+de\s+|la\s+ciudad\s+de\s+)?'
        r'(' + MUNI + r')'
        r'(?:,|;|\.|\s+calle|\s+c/|\s+en\s)',
        desc, re.IGNORECASE
    )
    if m:
        # Eliminar cualquier "en la..." que haya podido colarse al final
        muni = re.sub(r'\s+en\b.*', '', m.group(1)).strip()
        if muni.lower() not in _FALSOS_POSITIVOS and len(muni) > 2:
            return _validar_muni(muni)

    # 3. "en MUNICIPIO, calle/plaza/av/cl/c/"
    m = re.search(
        r'\ben\s+(' + MUNI + r')'
        r'(?:,\s*(?:calle|c/|plaza|pza|av\b|cl\b|ps\b))',
        desc, re.IGNORECASE
    )
    if m:
        muni = m.group(1).strip()
        if muni.lower() not in _FALSOS_POSITIVOS and len(muni) > 2:
            return _validar_muni(muni)

    # 4. "de MUNICIPIO, calle/..." — ej: "de Sagunto, calle Toledo"
    m = re.search(
        r'\bde\s+(' + MUNI + r')'
        r'(?:,\s*(?:calle|c/|plaza|pza|cl\b|av\b))',
        desc, re.IGNORECASE
    )
    if m:
        muni = m.group(1).strip()
        if muni.lower() not in _FALSOS_POSITIVOS and len(muni) > 2:
            return _validar_muni(muni)

    # 5. Dirección AEAT sin CP: "CL/AV CALLE NUM[/S/N] MUNICIPIO VALENCIA"
    m = re.search(
        r'(?:CL|AV|CR|PL|PS|GL|TR|CM|CV|CT|BV|CJ|UR)[/\s]'
        r'.+?(?:\d+|S/N)\s+'
        r'(' + MUNI + r')'
        r'\s+VALENCIA\b',
        desc, re.IGNORECASE
    )
    if m:
        muni = m.group(1).strip()
        if muni.lower() not in _FALSOS_POSITIVOS and len(muni) > 2:
            return _validar_muni(muni)

    # 6. Último recurso: "en MUNICIPIO" sin importar lo que sigue
    m = re.search(r'\ben\s+(' + MUNI + r')(?:,|\.|\s*$)', desc, re.IGNORECASE)
    if m:
        muni = m.group(1).strip()
        if muni.lower() not in _FALSOS_POSITIVOS and len(muni) > 2:
            return _validar_muni(muni)

    return ''


def _validar_muni(muni):
    """Limpia y valida el municipio extraído."""
    if not muni:
        return ''
    # Quitar prefijos legales
    muni = re.sub(r'^t[eé]rmino\s+(?:municipal\s+)?de\s+', '', muni, flags=re.IGNORECASE).strip()
    # Cortar en palabras ajenas al nombre del municipio
    muni = re.split(r'\s+(?:tipo|n[uú]mero|num|planta|piso|puerta|portal|escalera)\b',
                    muni, flags=re.IGNORECASE)[0].strip()
    # Si queda vacío o en falsos positivos, descartar
    if not muni or muni.lower() in _FALSOS_POSITIVOS or len(muni) < 3:
        return ''
    return muni.title()


# ============================================================
# PARSEO DE RESULTADOS
# ============================================================
def parsear_subastas(html):
    """Extrae la lista de subastas del HTML de resultados."""
    soup = BeautifulSoup(html, 'html.parser')
    subastas = []

    # Cada subasta es un <li class="resultado-busqueda">
    items = soup.find_all('li', class_='resultado-busqueda')
    print(f'  -> {len(items)} inmuebles encontrados en Valencia')

    for item in items:
        subasta = {}

        # --- ID de subasta (h3) ---
        h3 = item.find('h3')
        subasta['titulo'] = h3.get_text(strip=True) if h3 else ''

        # --- Autoridad gestora (h4) ---
        h4 = item.find('h4')
        subasta['autoridad'] = h4.get_text(strip=True) if h4 else ''

        # --- Parrafos: estado/fecha y descripcion del bien ---
        parrafos = item.find_all('p')
        subasta['estado_txt'] = ''
        subasta['descripcion'] = ''

        for p in parrafos:
            txt = p.get_text(strip=True)
            if 'Estado:' in txt:
                subasta['estado_txt'] = txt
            elif txt:  # descripcion del bien (tipo + direccion)
                subasta['descripcion'] = txt

        # --- Fecha de conclusion (extraer del estado) ---
        fecha_match = re.search(r'(\d{2}/\d{2}/\d{4})', subasta['estado_txt'])
        subasta['fecha_fin'] = fecha_match.group(1) if fecha_match else 'No especificada'

        # Tambien buscar hora
        hora_match = re.search(r'(\d{2}:\d{2}:\d{2})', subasta['estado_txt'])
        if hora_match:
            subasta['fecha_fin'] += f' {hora_match.group(1)}'

        # --- URL ---
        link = item.find('a', class_='resultado-busqueda-link-defecto')
        if link:
            href = link.get('href', '')
            if href and not href.startswith('http'):
                href = BASE_URL + '/' + href.lstrip('./')
            subasta['url'] = href
        else:
            subasta['url'] = ''

        # --- ID de subasta (del URL) ---
        id_match = re.search(r'idSub=([^&]+)', subasta.get('url', ''))
        subasta['id'] = id_match.group(1) if id_match else ''

        # --- Filtro vivienda ---
        texto_lower = subasta['descripcion'].lower()
        subasta['es_vivienda'] = any(kw in texto_lower for kw in KEYWORDS_VIVIENDA)

        # --- Municipio ---
        subasta['municipio'] = extraer_municipio(subasta['descripcion'])

        subastas.append(subasta)

    return subastas


# ============================================================
# RESUMEN LEGIBLE DE CADA SUBASTA
# ============================================================
TIPOS_BIEN = [
    (r'vivienda\s+unifamiliar',  'Casa unifamiliar'),
    (r'vivienda',                'Vivienda'),
    (r'\bpiso\b',                'Piso'),
    (r'\b[aá]tico\b',            'Ático'),
    (r'd[uú]plex',               'Dúplex'),
    (r'estudio',                 'Estudio'),
    (r'local\s+comercial',       'Local comercial'),
    (r'nave\s+industrial',       'Nave industrial'),
    (r'nave',                    'Nave'),
    (r'garaje|plaza\s+de\s+aparcamiento', 'Garaje'),
    (r'trastero',                'Trastero'),
    (r'\bsolar\b',               'Solar'),
    (r'finca\s+r[uú]stica',      'Finca rústica'),
    (r'oficina',                 'Oficina'),
]

PLANTAS = [
    (r'planta\s+baja',           'Planta baja'),
    (r'planta\s+primera|primera\s+planta|1[aª]\s+planta',  'Planta 1ª'),
    (r'planta\s+segunda|segunda\s+planta|2[aª]\s+planta',  'Planta 2ª'),
    (r'planta\s+tercera|tercera\s+planta|3[aª]\s+planta',  'Planta 3ª'),
    (r'planta\s+cuarta|cuarta\s+planta|4[aª]\s+planta',    'Planta 4ª'),
    (r'planta\s+quinta|quinta\s+planta|5[aª]\s+planta',    'Planta 5ª'),
    (r'planta\s+sexta|sexta\s+planta|6[aª]\s+planta',      'Planta 6ª'),
    (r'planta\s+s[eé]ptima|7[aª]\s+planta',                'Planta 7ª'),
    (r'planta\s+octava|8[aª]\s+planta',                    'Planta 8ª'),
    (r'planta\s+alta',           'Planta alta'),
    (r'bajo\s+cubierta|[aá]tico|buhardilla',               'Ático'),
]

def resumir(descripcion):
    """Genera un resumen de 1 línea a partir de la descripción del bien."""
    if not descripcion:
        return ''

    txt = descripcion.lower()
    partes = []

    # --- Tipo de bien ---
    tipo = 'Inmueble'
    for patron, etiqueta in TIPOS_BIEN:
        if re.search(patron, txt, re.IGNORECASE):
            tipo = etiqueta
            break
    partes.append(tipo)

    # --- Planta ---
    for patron, etiqueta in PLANTAS:
        if re.search(patron, txt, re.IGNORECASE):
            partes.append(etiqueta)
            break

    # --- Metros cuadrados ---
    m2 = re.search(
        r'(\d[\d\.,]+)\s*metros?\s*cuadrados?|'
        r'superficie[^:]*:\s*(\d[\d\.,]+)\s*m|'
        r'(\d[\d\.,]+)\s*m[²2]',
        txt, re.IGNORECASE
    )
    if m2:
        val = next(v for v in m2.groups() if v)
        # Normalizar: quitar decimales si son ,00
        val = val.replace('.', '').replace(',', '.')
        try:
            num = float(val)
            partes.append(f'{num:g} m²')
        except ValueError:
            pass

    # --- Municipio ---
    # Formato AEAT: "... . CP - MUNICIPIO (VALENCIA)"
    muni_aeat = re.search(r'\d{5}\s*-\s*([^()\n,]+)', descripcion)
    if muni_aeat:
        municipio = muni_aeat.group(1).strip().title()
        partes.append(municipio)
    else:
        # Texto libre: buscar "en MUNICIPIO," o "en MUNICIPIO."
        muni_libre = re.search(
            r'\ben\s+([A-ZÁÉÍÓÚÜÑ][a-záéíóúüñA-ZÁÉÍÓÚÜÑ\s]{2,25?})'
            r'(?:,|\.|calle|c/)',
            descripcion
        )
        if muni_libre:
            partes.append(muni_libre.group(1).strip().title())

    return ' · '.join(partes)


# ============================================================
# GOOGLE MAPS
# ============================================================
def url_google_maps(descripcion):
    """Genera una URL de búsqueda en Google Maps a partir de la descripción."""
    if not descripcion:
        return None

    from urllib.parse import quote_plus

    # Formato AEAT estructurado: "TIPO. CL/ CALLE, NUM. CP - MUNICIPIO (PROV)"
    # Intentar extraer: calle + CP + municipio
    match = re.match(
        r'^[^.]+\.\s*'               # tipo de bien (VIVIENDA., LOCAL., etc.)
        r'(?:CL|AV|CR|PL|PS|GL|TR|RD|CM|CV|CT|BV|CJ|UR|SN)[/\s]*'  # tipo vía
        r'(.+?)\.\s*'                # nombre calle + numero
        r'(\d{5})\s*-\s*'           # código postal
        r'([^(]+)',                  # municipio
        descripcion, re.IGNORECASE
    )
    if match:
        calle   = match.group(1).strip().rstrip(',')
        cp      = match.group(2)
        municipio = match.group(3).strip()
        query = f'{calle}, {cp} {municipio}, Valencia'
    else:
        # Texto libre judicial: usar la descripción entera (Google Maps lo resuelve bien)
        query = descripcion[:200]

    return f'https://www.google.com/maps/search/?api=1&query={quote_plus(query)}'


# ============================================================
# PRECIOS: descarga paralela de paginas de detalle
# ============================================================
CAMPOS_PRECIO = {
    'tasación':          'tasacion',
    'valor subasta':     'valor_subasta',
    'puja mínima':       'puja_minima',
    'importe del depósito': 'deposito',
    'cantidad reclamada': 'cantidad_reclamada',
}

def obtener_precios(subasta_id):
    """Descarga la pagina de detalle y extrae los campos de precio."""
    url = f'{BASE_URL}/detalleSubasta.php?idSub={subasta_id}'
    try:
        r = requests.get(url, timeout=15,
                         headers={'User-Agent': 'Mozilla/5.0',
                                  'Referer': BASE_URL})
        r.encoding = 'utf-8'
        soup = BeautifulSoup(r.text, 'html.parser')
        precios = {}
        for tr in soup.find_all('tr'):
            th = tr.find('th')
            td = tr.find('td')
            if not th or not td:
                continue
            clave = th.get_text(strip=True).lower()
            valor = td.get_text(strip=True)
            for patron, campo in CAMPOS_PRECIO.items():
                if patron in clave:
                    precios[campo] = valor
                    break
        return subasta_id, precios
    except Exception:
        return subasta_id, {}


def enriquecer_con_precios(subastas):
    """Descarga precios de todos los detalles en paralelo."""
    ids = [s['id'] for s in subastas if s.get('id')]
    print(f'  -> Descargando precios de {len(ids)} subastas...')

    mapa = {}
    with ThreadPoolExecutor(max_workers=10) as pool:
        futuros = {pool.submit(obtener_precios, sid): sid for sid in ids}
        completados = 0
        for futuro in as_completed(futuros):
            sid, precios = futuro.result()
            mapa[sid] = precios
            completados += 1
            print(f'  {completados}/{len(ids)}', end='\r')

    print()
    for s in subastas:
        s.update(mapa.get(s.get('id', ''), {}))
    return subastas


# ============================================================
# GENERACION DEL INFORME HTML
# ============================================================
def generar_html(subastas, fecha, page_title='Subastas BOE Valencia', historico=None, historico_base='', nav_links=None):
    """Genera el informe HTML completo."""
    fecha_str = fecha.strftime('%d/%m/%Y a las %H:%M')
    fecha_archivo = fecha.strftime('%d-%m-%Y')
    hoy = fecha.date()
    n_total = len(subastas)
    n_viviendas = sum(1 for s in subastas if s.get('es_vivienda'))

    nav_html = ''
    if nav_links:
        enlaces = ''.join(
            f'<a class="nav-link" href="{href}">{label}</a>'
            for label, href in nav_links
        )
        nav_html = f'<div class="top-nav">{enlaces}</div>'

    def fmt_fecha_legible(fecha_fin_str):
        """Convierte '13/04/2026 18:00:00' en texto legible con alerta si vence hoy."""
        if not fecha_fin_str or fecha_fin_str == 'No especificada':
            return fecha_fin_str, False
        try:
            # Parsear fecha (con o sin hora)
            for fmt in ('%d/%m/%Y %H:%M:%S', '%d/%m/%Y'):
                try:
                    dt = datetime.strptime(fecha_fin_str.strip(), fmt)
                    break
                except ValueError:
                    continue
            else:
                return fecha_fin_str, False

            vence_hoy = dt.date() == hoy
            manana    = (hoy.toordinal() + 1) == dt.date().toordinal()

            if vence_hoy:
                texto = f'Hoy a las {dt.strftime("%H:%M")}'
            elif manana:
                texto = f'Mañana a las {dt.strftime("%H:%M")}'
            else:
                meses = ['ene','feb','mar','abr','may','jun',
                         'jul','ago','sep','oct','nov','dic']
                texto = f'{dt.day} {meses[dt.month-1]} · {dt.strftime("%H:%M")}'
            return texto, vence_hoy
        except Exception:
            return fecha_fin_str, False

    def fmt_precio(valor):
        """Limpia y formatea un valor monetario."""
        if not valor or valor.lower() in ('sin puja mínima', 'sin tramos', ''):
            return None
        return valor.replace('\xa0', ' ').strip()

    def card(s):
        url = s.get('url', '#')
        titulo = s.get('titulo', 'Sin titulo')
        descripcion = s.get('descripcion', '')
        autoridad = s.get('autoridad', '')
        fecha_fin = s.get('fecha_fin', 'No especificada')
        sid = s.get('id', '')
        es_viv = s.get('es_vivienda', False)

        tasacion    = fmt_precio(s.get('tasacion', ''))
        valor_sub   = fmt_precio(s.get('valor_subasta', ''))
        puja_min    = fmt_precio(s.get('puja_minima', ''))
        deposito    = fmt_precio(s.get('deposito', ''))

        badge    = '<span class="badge">VIVIENDA</span>' if es_viv else ''
        border   = 'card-viv' if es_viv else 'card-otro'
        maps_url = url_google_maps(descripcion)
        resumen  = resumir(descripcion)
        fecha_txt, es_urgente = fmt_fecha_legible(fecha_fin)
        chip_fecha_cls = 'chip chip-fecha chip-urgente' if es_urgente else 'chip chip-fecha'
        icono_urgente  = ' 🔴' if es_urgente else ''

        # Bloque de precios — solo si hay datos
        precio_principal = tasacion or valor_sub
        bloque_precio = ''
        if precio_principal or puja_min or deposito:
            filas = ''
            if precio_principal:
                filas += f'<div class="precio-item"><span class="precio-label">Tasación</span><span class="precio-val">{precio_principal}</span></div>'
            if puja_min:
                filas += f'<div class="precio-item"><span class="precio-label">Puja mínima</span><span class="precio-val puja">{puja_min}</span></div>'
            if deposito:
                filas += f'<div class="precio-item"><span class="precio-label">Depósito</span><span class="precio-val deposito">{deposito}</span></div>'
            bloque_precio = f'<div class="precios">{filas}</div>'

        # Descripcion con truncado si supera 260 caracteres
        LIMITE = 260
        desc_escaped = descripcion.replace('"', '&quot;')
        if len(descripcion) > LIMITE:
            corte = descripcion[:LIMITE].rsplit(' ', 1)[0]  # cortar por palabra
            corte_escaped = corte.replace('"', '&quot;')
            bloque_desc = (
                f'<p class="desc-bien">'
                f'<span class="desc-corta">{corte_escaped}… '
                f'<button class="btn-expandir" onclick="expandir(this)">ver más ▾</button>'
                f'</span>'
                f'<span class="desc-completa" style="display:none">{desc_escaped} '
                f'<button class="btn-expandir" onclick="colapsar(this)">ver menos ▴</button>'
                f'</span>'
                f'</p>'
            )
        else:
            bloque_desc = f'<p class="desc-bien">{desc_escaped}</p>'

        bloque_resumen = f'<p class="resumen">{resumen}</p>' if resumen else ''
        municipio = s.get('municipio', '')
        data_muni = f'data-municipio="{municipio}"' if municipio else ''

        return f"""
        <div class="card {border}" {data_muni}>
          <div class="card-head">
            {badge}
            <span class="card-id">{titulo}</span>
          </div>
          {bloque_resumen}
          {bloque_desc}
          <p class="autoridad">{autoridad}</p>
          {bloque_precio}
          <div class="chips">
            <span class="{chip_fecha_cls}">Fin: {fecha_txt}{icono_urgente}</span>
            {'<span class="chip chip-id">'+sid+'</span>' if sid else ''}
          </div>
          <div class="botones">
            {'<a href="'+url+'" target="_blank" class="btn-boe">Ver en BOE →</a>' if url and url != '#' else ''}
            {'<a href="'+maps_url+'" target="_blank" class="btn-maps">📍 Ver en Maps</a>' if maps_url else ''}
          </div>
        </div>"""

    viviendas = [s for s in subastas if s.get('es_vivienda')]
    otros = [s for s in subastas if not s.get('es_vivienda')]

    sec_viviendas = ''
    if viviendas:
        sec_viviendas = (
            f'<h2 class="sec-titulo">Viviendas ({len(viviendas)})</h2>'
            + '\n'.join(card(s) for s in viviendas)
        )

    sec_otros = ''
    if otros:
        sec_otros = (
            f'<h2 class="sec-titulo otros-titulo">Otros inmuebles ({len(otros)})</h2>'
            + '\n'.join(card(s) for s in otros)
        )

    if not subastas:
        contenido = '<div class="empty">No se encontraron subastas de inmuebles activas en Valencia.</div>'
    else:
        contenido = sec_viviendas + sec_otros

    # Barra de filtros: municipios con al menos 1 subasta, ordenados
    municipios = sorted(set(
        s['municipio'] for s in subastas if s.get('municipio')
    ))
    botones_filtro = ''.join(
        f'<button class="filtro" onclick="filtrar(this)" data-muni="{m}">{m}</button>'
        for m in municipios
    )
    barra_filtros = f"""
    <div class="filtros-wrap">
      <span class="filtros-label">Filtrar por municipio:</span>
      <div class="filtros">
        <button class="filtro activo" onclick="filtrar(this)" data-muni="">Todos</button>
        {botones_filtro}
      </div>
    </div>""" if municipios else ''

    historial_html = ''
    if historico:
        enlaces = ''.join(
            f"<a class=\"hist-link\" href=\"{historico_base}{h}\" target=\"_blank\">{os.path.basename(h).replace('.html', '')}</a>"
            for h in historico
        )
        historial_html = f"""
        <div class="historial">
          <h2>Informes anteriores</h2>
          <div class="hist-list">{enlaces}</div>
        </div>"""

    return f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>SubastaViva · {page_title}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:'Segoe UI',Tahoma,sans-serif;background:#f3f4f6;color:#1a1a1a}}
.top{{background:#b71c1c;color:#fff;padding:22px 20px;text-align:center}}
.top-logo{{font-size:13px;font-weight:800;letter-spacing:.5px;text-transform:uppercase;
  opacity:.75;margin-bottom:6px}}
.top h1{{font-size:22px;margin-bottom:5px}}
.top p{{font-size:12px;opacity:.85}}
.top a{{color:#ffcdd2;text-decoration:none}}
.stats{{background:#fff;border-bottom:1px solid #e5e7eb;display:flex;
  justify-content:center;gap:40px;padding:14px;flex-wrap:wrap}}
.stat strong{{display:block;font-size:26px;color:#b71c1c;text-align:center}}
.stat span{{font-size:11px;color:#6b7280;display:block;text-align:center}}
.wrap{{max-width:820px;margin:24px auto;padding:0 14px}}
.sec-titulo{{font-size:14px;font-weight:700;color:#374151;text-transform:uppercase;
  letter-spacing:.5px;margin:24px 0 12px;padding-bottom:6px;
  border-bottom:2px solid #b71c1c}}
.otros-titulo{{border-color:#d1d5db;color:#9ca3af}}
.card{{background:#fff;border-radius:8px;margin-bottom:14px;
  padding:14px 18px;box-shadow:0 1px 4px rgba(0,0,0,.08)}}
.card-viv{{border-left:4px solid #b71c1c}}
.card-otro{{border-left:4px solid #d1d5db}}
.card-head{{display:flex;align-items:center;gap:8px;margin-bottom:6px;flex-wrap:wrap}}
.badge{{background:#b71c1c;color:#fff;font-size:10px;font-weight:700;
  padding:2px 7px;border-radius:3px;letter-spacing:.4px}}
.card-id{{font-size:12px;font-weight:600;color:#6b7280}}
.resumen{{font-size:15px;font-weight:700;color:#111827;margin-bottom:6px}}
.desc-bien{{font-size:13px;color:#6b7280;margin-bottom:4px;line-height:1.5}}
.autoridad{{font-size:12px;color:#6b7280;margin-bottom:10px}}
.chips{{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px}}
.chip{{font-size:11px;padding:3px 9px;border-radius:12px}}
.chip-fecha{{background:#fef2f2;color:#b91c1c;font-weight:600}}
.chip-urgente{{background:#b71c1c!important;color:#fff!important;animation:parpadeo 1.5s infinite}}
@keyframes parpadeo{{0%,100%{{opacity:1}}50%{{opacity:.7}}}}
.chip-id{{background:#f3f4f6;color:#6b7280}}
.top-nav{{background:#fff;border-bottom:1px solid #e5e7eb;display:flex;justify-content:center;gap:12px;padding:10px 0}}
.nav-link{{font-size:13px;color:#374151;text-decoration:none;font-weight:700;padding:8px 16px;border-radius:999px;transition:background .2s ease,color .2s ease}}
.nav-link:hover{{background:#fef2f2;color:#b91c1c}}
.filtros-wrap{{background:#fff;border-bottom:1px solid #e5e7eb;
  padding:12px 20px;display:flex;align-items:center;gap:12px;flex-wrap:wrap}}
.filtros-label{{font-size:12px;font-weight:600;color:#6b7280;white-space:nowrap}}
.filtros{{display:flex;gap:6px;flex-wrap:wrap}}
.filtro{{padding:5px 13px;border-radius:20px;border:1.5px solid #e5e7eb;
  background:#fff;font-size:12px;cursor:pointer;color:#374151;font-weight:500}}
.filtro:hover{{border-color:#b71c1c;color:#b71c1c}}
.filtro.activo{{background:#b71c1c;border-color:#b71c1c;color:#fff}}
.precios{{display:flex;gap:10px;flex-wrap:wrap;margin:10px 0;
  background:#fafafa;border-radius:6px;padding:10px 12px;border:1px solid #f3f4f6}}
.precio-item{{display:flex;flex-direction:column;min-width:130px}}
.precio-label{{font-size:10px;text-transform:uppercase;letter-spacing:.4px;
  color:#9ca3af;margin-bottom:2px}}
.precio-val{{font-size:17px;font-weight:700;color:#111827}}
.precio-val.puja{{color:#b71c1c}}
.precio-val.deposito{{font-size:13px;font-weight:600;color:#374151}}
.botones{{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}}
.btn-boe{{display:inline-block;padding:7px 16px;background:#b71c1c;color:#fff;
  text-decoration:none;border-radius:5px;font-size:12px;font-weight:500}}
.btn-boe:hover{{background:#7f1d1d}}
.btn-maps{{display:inline-block;padding:7px 16px;background:#1a73e8;color:#fff;
  text-decoration:none;border-radius:5px;font-size:12px;font-weight:500}}
.btn-maps:hover{{background:#1558b0}}
.btn-expandir{{background:none;border:none;color:#1a73e8;font-size:12px;
  cursor:pointer;padding:0;font-weight:600}}
.btn-expandir:hover{{text-decoration:underline}}
.empty{{background:#fff;border-radius:8px;padding:50px;text-align:center;color:#9ca3af}}
.footer{{text-align:center;padding:20px;font-size:11px;color:#9ca3af}}
.footer a{{color:#b71c1c;text-decoration:none}}
.historial{{background:#fff;border-radius:8px;padding:16px;margin:24px 0;box-shadow:0 1px 4px rgba(0,0,0,.08)}}
.historial h2{{font-size:14px;color:#374151;margin-bottom:10px}}
.hist-list{{display:flex;flex-wrap:wrap;gap:8px}}
.hist-link{{display:inline-block;padding:8px 12px;background:#f3f4f6;color:#1f2937;text-decoration:none;border-radius:6px;border:1px solid #e5e7eb;font-size:12px}}
.hist-link:hover{{background:#e5e7eb}}
</style>
</head>
<body>
<div class="top">
  <div class="top-logo">SubastaViva</div>
  <h1>{page_title}</h1>
  <p>Actualizado el {fecha_str} · <a href="https://subastas.boe.es" target="_blank">subastas.boe.es</a></p>
</div>
{nav_html}
<div class="stats">
  <div class="stat"><strong>{n_viviendas}</strong><span>Viviendas</span></div>
  <div class="stat"><strong>{n_total}</strong><span>Total inmuebles</span></div>
</div>
{barra_filtros}
<div class="wrap" id="listado">
{contenido}
</div>
<div class="footer">
  SubastaViva · Datos de <a href="https://subastas.boe.es" target="_blank">subastas.boe.es</a> · Solo uso informativo
</div>
<script>
function expandir(btn) {{
  var p = btn.closest('p');
  p.querySelector('.desc-corta').style.display = 'none';
  p.querySelector('.desc-completa').style.display = '';
}}
function colapsar(btn) {{
  var p = btn.closest('p');
  p.querySelector('.desc-corta').style.display = '';
  p.querySelector('.desc-completa').style.display = 'none';
}}
function filtrar(btn) {{
  document.querySelectorAll('.filtro').forEach(b => b.classList.remove('activo'));
  btn.classList.add('activo');
  var muni = btn.dataset.muni;
  document.querySelectorAll('#listado .card').forEach(function(card) {{
    if (!muni || card.dataset.municipio === muni) {{
      card.style.display = '';
    }} else {{
      card.style.display = 'none';
    }}
  }});
  // Ocultar titulos de seccion si todos sus hijos están ocultos
  document.querySelectorAll('#listado .sec-titulo').forEach(function(h) {{
    var next = h.nextElementSibling;
    var visible = false;
    while (next && !next.classList.contains('sec-titulo')) {{
      if (next.classList.contains('card') && next.style.display !== 'none') visible = true;
      next = next.nextElementSibling;
    }}
    h.style.display = visible ? '' : 'none';
  }});
}}
</script>
</body>
</html>""", f'Subastas_Valencia_{fecha_archivo}.html'


def guardar_json(subastas, fecha):
    """Guarda los datos de subastas en JSON diario y latest."""
    fecha_archivo = fecha.strftime('%d-%m-%Y')
    datos = {
        'generado': fecha.isoformat(),
        'fecha': fecha.strftime('%d/%m/%Y %H:%M:%S'),
        'subastas': subastas,
    }
    ruta_diaria = os.path.join(CARPETA_INFORMES, f'Subastas_Valencia_{fecha_archivo}.json')
    ruta_latest = os.path.join(CARPETA_INFORMES, 'latest.json')
    with open(ruta_diaria, 'w', encoding='utf-8') as f:
        json.dump(datos, f, ensure_ascii=False, indent=2)
    with open(ruta_latest, 'w', encoding='utf-8') as f:
        json.dump(datos, f, ensure_ascii=False, indent=2)
    return ruta_diaria, ruta_latest


def listar_informes_historicos():
    """Devuelve la lista de informes HTML diarios ordenados de más reciente a más antiguo."""
    archivos = [f for f in os.listdir(CARPETA_INFORMES)
                if f.startswith('Subastas_Valencia_') and f.endswith('.html')]
    archivos.sort(reverse=True)
    return archivos


# ============================================================
# CONVERSION A PDF
# ============================================================
import glob as _glob
NAVEGADORES = [
    r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
    r'C:\Program Files\Microsoft\Edge\Application\msedge.exe',
    r'C:\Users\nacho\AppData\Local\Microsoft\Edge\Application\msedge.exe',
    r'C:\Program Files\Google\Chrome\Application\chrome.exe',
    r'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe',
    r'C:\Users\nacho\AppData\Local\Google\Chrome\Application\chrome.exe',
]

def html_a_pdf(ruta_html, ruta_pdf):
    """Convierte el HTML a PDF usando Edge o Chrome en modo headless."""
    navegador = next((b for b in NAVEGADORES if os.path.exists(b)), None)
    if not navegador:
        print('  [!] No se encontro Edge ni Chrome para generar el PDF.')
        return False

    url = 'file:///' + ruta_html.replace('\\', '/')
    cmd = [
        navegador,
        '--headless=new',
        '--disable-gpu',
        '--no-sandbox',
        '--print-to-pdf-no-header',
        '--print-to-pdf=' + ruta_pdf.replace('\\', '/'),
        url,
    ]

    try:
        subprocess.run(cmd, timeout=30, capture_output=True)
        time.sleep(3)
        if os.path.exists(ruta_pdf) and os.path.getsize(ruta_pdf) > 1000:
            return True
        print('  [!] El PDF se genero vacio o con error.')
        return False
    except subprocess.TimeoutExpired:
        print('  [!] Timeout al generar el PDF.')
        return False
    except Exception as e:
        print('  [!] Error al generar PDF: ' + str(e))
        return False


# ============================================================
# MAIN
# ============================================================
def main():
    print('=' * 50)
    print('  BOE Subastas Valencia')
    print(f'  {datetime.now().strftime("%d/%m/%Y %H:%M:%S")}')
    print('=' * 50)

    session = requests.Session()

    # Cargar cookies iniciales
    try:
        session.get(SEARCH_URL, timeout=10,
                    headers={'User-Agent': 'Mozilla/5.0'})
    except Exception:
        pass

    print('\n[1/3] Buscando en subastas.boe.es...')
    html, error = buscar_subastas(session)

    if error:
        print(f'ERROR: No se pudo conectar: {error}')
        sys.exit(1)

    # Debug: guardar HTML crudo
    if '--debug' in sys.argv:
        ruta_debug = os.path.join(CARPETA_INFORMES, 'boe_debug.html')
        with open(ruta_debug, 'w', encoding='utf-8') as f:
            f.write(html)
        print(f'  [debug] HTML guardado en {ruta_debug}')

    # Comprobar error del BOE
    if 'Se ha producido un error' in html:
        print('ERROR: El portal BOE devolvio un error.')
        ruta_debug = os.path.join(CARPETA_INFORMES, 'boe_debug.html')
        with open(ruta_debug, 'w', encoding='utf-8') as f:
            f.write(html)
        print(f'  HTML guardado en: {ruta_debug}')
        sys.exit(1)

    print('[2/3] Procesando resultados...')
    subastas = parsear_subastas(html)

    n_viv = sum(1 for s in subastas if s.get('es_vivienda'))
    print(f'  -> {n_viv} identificadas como viviendas')

    print('[2.5/3] Obteniendo precios...')
    subastas = enriquecer_con_precios(subastas)

    fecha_ahora = datetime.now()
    html_informe, nombre = generar_html(
        subastas,
        fecha_ahora,
        nav_links=[('Inicio', '../web/index.html'), ('Subastas', '../web/subastas.html'), ('Histórico', 'index.html')]
    )

    ruta = os.path.join(CARPETA_INFORMES, nombre)
    with open(ruta, 'w', encoding='utf-8') as f:
        f.write(html_informe)

    ruta_json_diaria, ruta_json_latest = guardar_json(subastas, fecha_ahora)
    print(f'  JSON guardado en: {ruta_json_diaria}')
    print(f'  JSON latest actualizado en: {ruta_json_latest}')

    historico = listar_informes_historicos()
    html_index, _ = generar_html(
        subastas,
        fecha_ahora,
        page_title='Subastas Valencia · Histórico',
        historico=historico,
        nav_links=[('Inicio', '../web/index.html'), ('Subastas', '../web/subastas.html'), ('Histórico', 'index.html')],
    )
    ruta_index = os.path.join(CARPETA_INFORMES, 'index.html')
    with open(ruta_index, 'w', encoding='utf-8') as f:
        f.write(html_index)

    html_subastas, _ = generar_html(
        subastas,
        fecha_ahora,
        page_title='Subastas Valencia · Activas',
        historico=historico,
        historico_base='../informes/',
        nav_links=[('Inicio', 'index.html'), ('Subastas', 'subastas.html'), ('Histórico', '../informes/index.html')],
    )
    ruta_web_subastas = os.path.join(CARPETA_WEB, 'subastas.html')
    with open(ruta_web_subastas, 'w', encoding='utf-8') as f:
        f.write(html_subastas)

    ruta_web_json = os.path.join(CARPETA_WEB, 'latest.json')
    with open(ruta_web_json, 'w', encoding='utf-8') as f:
        json.dump({
            'generado': fecha_ahora.isoformat(),
            'fecha': fecha_ahora.strftime('%d/%m/%Y %H:%M:%S'),
            'subastas': subastas,
        }, f, ensure_ascii=False, indent=2)

    print(f'  Página índice actualizada en: {ruta_index}')
    print(f'  Página de subastas actualizada en: {ruta_web_subastas}')
    print(f'  JSON web latest actualizado en: {ruta_web_json}')
    print(f'\n  HTML guardado en: {ruta}')
    if sys.stdout.isatty():
        webbrowser.open(f'file:///{ruta.replace(chr(92), "/")}')
        print('  Abierto en el navegador.')

    print('\nListo.')


if __name__ == '__main__':
    main()
