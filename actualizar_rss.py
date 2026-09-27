import hashlib
import html
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import urljoin
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup


USUARIO = "plis2100"
REPOSITORIO = "epo-nuevas-publicaciones-rss"

BASE = "https://data.epo.org/publication-server/rest/v1.2"
URL_FECHAS = f"{BASE}/publication-dates"

URL_RSS = (
    f"https://raw.githubusercontent.com/{USUARIO}/"
    f"{REPOSITORIO}/main/epo-feed-v2.xml"
)

ARCHIVO_RSS = Path("epo-feed-v2.xml")
ARCHIVO_HISTORIAL = Path("historial.json")

MAX_PUBLICACIONES = 1000
MAX_ENTRADAS = 2000
TRABAJADORES = 8

MADRID = ZoneInfo("Europe/Madrid")

CABECERAS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "Chrome/136.0 Safari/537.36"
    ),
    "Accept": "application/xml,text/xml,text/html,*/*",
    "Accept-Language": "en,es;q=0.9",
}


def limpiar(valor):
    return " ".join(str(valor or "").split()).strip()


def local(etiqueta):
    return etiqueta.rsplit("}", 1)[-1]


def elementos(raiz, nombre):
    return [
        elemento
        for elemento in raiz.iter()
        if local(elemento.tag).lower() == nombre.lower()
    ]


def crear_sesion():
    sesion = requests.Session()
    sesion.headers.update(CABECERAS)
    return sesion


def descargar(sesion, url, intentos=3):
    ultimo_error = None

    for intento in range(1, intentos + 1):
        try:
            respuesta = sesion.get(
                url,
                timeout=(15, 90),
            )

            respuesta.raise_for_status()

            if not respuesta.content:
                raise RuntimeError("Respuesta vacía")

            return respuesta

        except (
            requests.RequestException,
            RuntimeError,
        ) as error:
            ultimo_error = error

            if intento < intentos:
                time.sleep(intento * 2)

    raise RuntimeError(
        f"No se pudo descargar {url}: {ultimo_error}"
    )


def obtener_ultima_fecha():
    respuesta = descargar(
        crear_sesion(),
        URL_FECHAS,
    )

    fechas = set(
        re.findall(
            r"(?:publication-dates/|\b)"
            r"(20\d{6})(?:/|\b)",
            respuesta.text,
        )
    )

    hoy = datetime.now(
        MADRID
    ).strftime("%Y%m%d")

    fechas_validas = [
        fecha
        for fecha in fechas
        if fecha <= hoy
    ]

    if not fechas_validas:
        raise RuntimeError(
            "No se encontraron fechas de publicación EPO"
        )

    fecha = max(fechas_validas)

    print(f"Última fecha EPO: {fecha}")

    return fecha


def obtener_listado(fecha):
    url = (
        f"{BASE}/publication-dates/"
        f"{fecha}/patents"
    )

    respuesta = descargar(
        crear_sesion(),
        url,
    )

    sopa = BeautifulSoup(
        respuesta.text,
        "html.parser",
    )

    resultado = []
    vistos = set()

    for enlace in sopa.find_all(
        "a",
        href=True,
    ):
        destino = urljoin(
            respuesta.url,
            enlace["href"],
        )

        coincidencia = re.search(
            r"/patents/([^/?#]+)$",
            destino,
            re.IGNORECASE,
        )

        if not coincidencia:
            continue

        identificador = coincidencia.group(1)

        if identificador not in vistos:
            vistos.add(identificador)
            resultado.append(identificador)

    if not resultado:
        raise RuntimeError(
            f"No hay publicaciones para {fecha}"
        )

    print(
        f"Publicaciones encontradas: {len(resultado)}"
    )

    if len(resultado) > MAX_PUBLICACIONES:
        print(
            f"Se procesarán las primeras "
            f"{MAX_PUBLICACIONES} publicaciones"
        )

    return resultado[:MAX_PUBLICACIONES]


def obtener_codigo(raiz, identificador):
    codigo = limpiar(
        raiz.attrib.get("kind")
    ).upper()

    if codigo:
        return codigo

    coincidencia = re.search(
        r"(A1|A2|A3|A8|A9|"
        r"B1|B2|B3|B8|B9)$",
        identificador,
        re.IGNORECASE,
    )

    if coincidencia:
        return coincidencia.group(1).upper()

    return ""


def obtener_numero(raiz, identificador):
    numero = limpiar(
        raiz.attrib.get("doc-number")
    )

    if numero:
        return numero.lstrip("0") or "0"

    coincidencia = re.search(
        r"EP0*(\d+)",
        identificador,
        re.IGNORECASE,
    )

    if coincidencia:
        return coincidencia.group(1)

    return identificador


def obtener_fecha(raiz, alternativa):
    fecha = limpiar(
        raiz.attrib.get("date-publ")
    )

    if re.fullmatch(r"\d{8}", fecha):
        return fecha

    for elemento in raiz.iter():
        texto = limpiar(
            "".join(elemento.itertext())
        )

        coincidencia = re.search(
            r"\b(20\d{6})\b",
            texto,
        )

        if coincidencia:
            return coincidencia.group(1)

    return alternativa


def obtener_titulo(raiz):
    titulos = []

    for bloque in elementos(
        raiz,
        "B540",
    ):
        idioma = ""

        for elemento in bloque.iter():
            etiqueta = local(
                elemento.tag
            ).upper()

            texto = limpiar(
                "".join(elemento.itertext())
            )

            if etiqueta == "B541":
                idioma = texto.lower()

            elif etiqueta == "B542" and texto:
                titulos.append(
                    (idioma, texto)
                )

    for idioma, titulo in titulos:
        if idioma == "en":
            return titulo

    if titulos:
        return titulos[0][1]

    return "Título no disponible"


def obtener_nombres(
    raiz,
    nombre_bloque,
    nombre_persona,
):
    resultado = []
    vistos = set()

    for bloque in elementos(
        raiz,
        nombre_bloque,
    ):
        for persona in elementos(
            bloque,
            nombre_persona,
        ):
            for campo in elementos(
                persona,
                "snm",
            ):
                nombre = limpiar(
                    "".join(campo.itertext())
                )

                clave = nombre.casefold()

                if nombre and clave not in vistos:
                    vistos.add(clave)
                    resultado.append(nombre)

    return resultado


def clasificar_tipo(codigo):
    tipos = {
        "A1": "NUEVA SOLICITUD",
        "A2": "SOLICITUD SIN INFORME DE BÚSQUEDA",
        "A3": "INFORME DE BÚSQUEDA",
        "A8": "CORRECCIÓN DE SOLICITUD",
        "A9": "CORRECCIÓN DE SOLICITUD",
        "B1": "PATENTE CONCEDIDA",
        "B2": "PATENTE MODIFICADA",
        "B3": "PATENTE MODIFICADA",
        "B8": "CORRECCIÓN DE PATENTE",
        "B9": "CORRECCIÓN DE PATENTE",
    }

    return tipos.get(
        codigo,
        f"PUBLICACIÓN {codigo}".strip(),
    )


def procesar_publicacion(
    identificador,
    fecha_semanal,
):
    try:
        url_xml = (
            f"{BASE}/patents/"
            f"{identificador}/document.xml"
        )

        respuesta = descargar(
            crear_sesion(),
            url_xml,
        )

        raiz = ET.fromstring(
            respuesta.content
        )

        codigo = obtener_codigo(
            raiz,
            identificador,
        )

        numero = obtener_numero(
            raiz,
            identificador,
        )

        fecha = obtener_fecha(
            raiz,
            fecha_semanal,
        )

        titulo = obtener_titulo(raiz)

        solicitantes = obtener_nombres(
            raiz,
            "B710",
            "B711",
        )[:20]

        inventores = obtener_nombres(
            raiz,
            "B720",
            "B721",
        )[:30]

        solicitante_principal = (
            solicitantes[0]
            if solicitantes
            else "Solicitante no indicado"
        )

        tipo = clasificar_tipo(codigo)

        numero_completo = (
            f"EP{numero} {codigo}"
        ).strip()

        url_documento = (
            f"{BASE}/patents/"
            f"{identificador}/document.html"
        )

        fecha_visible = (
            f"{fecha[6:8]}/"
            f"{fecha[4:6]}/"
            f"{fecha[:4]}"
        )

        solicitantes_html = (
            "<br>".join(
                html.escape(nombre)
                for nombre in solicitantes
            )
            or "No indicado"
        )

        inventores_html = (
            "<br>".join(
                html.escape(nombre)
                for nombre in inventores
            )
            or "No indicados"
        )

        descripcion = (
            f"<p><strong>Tipo:</strong> "
            f"{html.escape(tipo)}</p>"

            f"<p><strong>Solicitante/titular:"
            f"</strong><br>{solicitantes_html}</p>"

            f"<p><strong>Inventores:</strong>"
            f"<br>{inventores_html}</p>"

            f"<p><strong>Título:</strong> "
            f"{html.escape(titulo)}</p>"

            f"<p><strong>Publicación:</strong> "
            f"{html.escape(numero_completo)}</p>"

            f"<p><strong>Fecha:</strong> "
            f"{fecha_visible}</p>"

            f'<p><a href="{html.escape(url_documento)}">'
            f"Abrir publicación oficial en la EPO"
            f"</a></p>"
        )

        fecha_datetime = datetime(
            int(fecha[:4]),
            int(fecha[4:6]),
            int(fecha[6:8]),
            14,
            0,
            tzinfo=MADRID,
        )

        identificador_rss = hashlib.sha256(
            (
                "epo-publicacion-v2|"
                f"{identificador}"
            ).encode("utf-8")
        ).hexdigest()

        return {
            "id": identificador_rss,
            "identificador_epo": identificador,
            "titulo": (
                f"{tipo} | "
                f"{solicitante_principal} | "
                f"{titulo} | "
                f"{numero_completo}"
            ),
            "url": url_documento,
            "descripcion": descripcion,
            "fecha": fecha_datetime.isoformat(),
            "tipo": tipo,
            "numero": numero_completo,
            "solicitantes": solicitantes,
            "inventores": inventores,
        }

    except Exception as error:
        print(
            f"ERROR {identificador}: {error}"
        )

        return None


def descargar_publicaciones(
    identificadores,
    fecha,
):
    publicaciones = []

    with ThreadPoolExecutor(
        max_workers=TRABAJADORES
    ) as ejecutor:

        tareas = [
            ejecutor.submit(
                procesar_publicacion,
                identificador,
                fecha,
            )
            for identificador in identificadores
        ]

        total = len(tareas)

        for numero, tarea in enumerate(
            as_completed(tareas),
            1,
        ):
            resultado = tarea.result()

            if resultado:
                publicaciones.append(resultado)

            if (
                numero % 25 == 0
                or numero == total
            ):
                print(
                    f"Procesadas: "
                    f"{numero}/{total}"
                )

    publicaciones.sort(
        key=lambda entrada: (
            (
                entrada.get("solicitantes")
                or [""]
            )[0].casefold(),
            entrada["numero"],
        )
    )

    return publicaciones


def cargar_historial():
    try:
        datos = json.loads(
            ARCHIVO_HISTORIAL.read_text(
                encoding="utf-8"
            )
        )

        if isinstance(datos, list):
            return datos

    except (
        OSError,
        json.JSONDecodeError,
    ):
        pass

    return []


def mezclar_publicaciones(
    nuevas,
    anteriores,
):
    publicaciones_unicas = {}

    for entrada in anteriores:
        identificador = entrada.get(
            "identificador_epo"
        )

        if identificador:
            publicaciones_unicas[
                identificador
            ] = entrada

    for entrada in nuevas:
        identificador = entrada.get(
            "identificador_epo"
        )

        if identificador:
            publicaciones_unicas[
                identificador
            ] = entrada

    resultado = list(
        publicaciones_unicas.values()
    )

    resultado.sort(
        key=lambda entrada: entrada.get(
            "fecha",
            "",
        ),
        reverse=True,
    )

    return resultado[:MAX_ENTRADAS]


def convertir_fecha_rss(fecha):
    return format_datetime(
        fecha.astimezone(timezone.utc)
    )


def crear_rss(publicaciones):
    atom = "http://www.w3.org/2005/Atom"

    ET.register_namespace(
        "atom",
        atom,
    )

    rss = ET.Element(
        "rss",
        {"version": "2.0"},
    )

    canal = ET.SubElement(
        rss,
        "channel",
    )

    ET.SubElement(
        canal,
        "title",
    ).text = (
        "Nuevas publicaciones EPO "
        "- solicitantes y títulos"
    )

    ET.SubElement(
        canal,
        "link",
    ).text = URL_RSS

    ET.SubElement(
        canal,
        "description",
    ).text = (
        "Publicaciones de la Oficina Europea "
        "de Patentes clasificadas por "
        "solicitante, título y número."
    )

    ET.SubElement(
        canal,
        "language",
    ).text = "es-ES"

    ET.SubElement(
        canal,
        "lastBuildDate",
    ).text = convertir_fecha_rss(
        datetime.now(timezone.utc)
    )

    ET.SubElement(
        canal,
        "ttl",
    ).text = "10080"

    ET.SubElement(
        canal,
        f"{{{atom}}}link",
        {
            "href": URL_RSS,
            "rel": "self",
            "type": "application/rss+xml",
        },
    )

    for publicacion in publicaciones:
        item = ET.SubElement(
            canal,
            "item",
        )

        ET.SubElement(
            item,
            "title",
        ).text = publicacion["titulo"]

        ET.SubElement(
            item,
            "link",
        ).text = publicacion["url"]

        ET.SubElement(
            item,
            "guid",
            {"isPermaLink": "false"},
        ).text = publicacion["id"]

        fecha = datetime.fromisoformat(
            publicacion["fecha"]
        )

        ET.SubElement(
            item,
            "pubDate",
        ).text = convertir_fecha_rss(fecha)

        ET.SubElement(
            item,
            "description",
        ).text = publicacion["descripcion"]

        ET.SubElement(
            item,
            "category",
        ).text = publicacion.get(
            "tipo",
            "EPO",
        )

    arbol = ET.ElementTree(rss)

    ET.indent(
        arbol,
        space="  ",
    )

    arbol.write(
        ARCHIVO_RSS,
        encoding="utf-8",
        xml_declaration=True,
    )

    ET.parse(ARCHIVO_RSS)

    print(
        f"{ARCHIVO_RSS} generado: "
        f"{ARCHIVO_RSS.stat().st_size} bytes"
    )


def main():
    print(
        "======================================"
    )
    print(
        "NUEVAS PUBLICACIONES EPO V2"
    )
    print(
        "======================================"
    )

    fecha = obtener_ultima_fecha()

    identificadores = obtener_listado(
        fecha
    )

    nuevas = descargar_publicaciones(
        identificadores,
        fecha,
    )

    if not nuevas:
        raise RuntimeError(
            "No se pudo procesar ninguna publicación"
        )

    anteriores = cargar_historial()

    todas = mezclar_publicaciones(
        nuevas,
        anteriores,
    )

    ARCHIVO_HISTORIAL.write_text(
        json.dumps(
            todas,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    crear_rss(todas)

    print(
        f"Entradas nuevas procesadas: "
        f"{len(nuevas)}"
    )

    print(
        f"Entradas totales en RSS: "
        f"{len(todas)}"
    )


if __name__ == "__main__":
    main()
