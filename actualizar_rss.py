import hashlib
import html
import json
import re
import sys
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


# ============================================================
# CONFIGURACIÓN
# ============================================================

USUARIO_GITHUB = "plis2100"
REPOSITORIO = "epo-nuevas-publicaciones-rss"

BASE_API = (
    "https://data.epo.org/"
    "publication-server/rest/v1.2"
)

URL_FECHAS = (
    f"{BASE_API}/publication-dates"
)

URL_EPO = (
    "https://data.epo.org/"
    "publication-server/"
)

URL_RSS = (
    "https://raw.githubusercontent.com/"
    f"{USUARIO_GITHUB}/{REPOSITORIO}/"
    "main/feed.xml"
)

ARCHIVO_RSS = Path("feed.xml")
ARCHIVO_HISTORIAL = Path("historial.json")

MAXIMO_PUBLICACIONES_SEMANA = 1000
MAXIMO_ENTRADAS_RSS = 2000
MAXIMO_TRABAJADORES = 8

ZONA_HORARIA = ZoneInfo("Europe/Madrid")

CABECERAS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/136.0 Safari/537.36"
    ),
    "Accept": (
        "application/xml,text/xml,text/html,"
        "application/xhtml+xml;q=0.9,*/*;q=0.8"
    ),
    "Accept-Language": "en-GB,en;q=0.9,es;q=0.8",
}


# ============================================================
# FUNCIONES AUXILIARES
# ============================================================

def limpiar(valor):
    if valor is None:
        return ""

    return " ".join(str(valor).split()).strip()


def fecha_rss(fecha):
    if fecha is None:
        fecha = datetime.now(timezone.utc)

    if fecha.tzinfo is None:
        fecha = fecha.replace(
            tzinfo=timezone.utc
        )

    return format_datetime(
        fecha.astimezone(timezone.utc)
    )


def fecha_iso_desde_epo(valor):
    texto = limpiar(valor)

    coincidencia = re.search(
        r"(\d{4})(\d{2})(\d{2})",
        texto,
    )

    if not coincidencia:
        return datetime.now(
            ZONA_HORARIA
        )

    anio, mes, dia = coincidencia.groups()

    return datetime(
        int(anio),
        int(mes),
        int(dia),
        14,
        0,
        tzinfo=ZONA_HORARIA,
    )


def nombre_local(etiqueta):
    if "}" in etiqueta:
        return etiqueta.split("}", 1)[1]

    return etiqueta


def elementos_por_nombre(raiz, nombre):
    resultado = []

    for elemento in raiz.iter():
        if nombre_local(
            elemento.tag
        ).lower() == nombre.lower():
            resultado.append(elemento)

    return resultado


def clasificar_tipo(codigo):
    codigo = limpiar(codigo).upper()

    if codigo.startswith("A1"):
        return "NUEVA SOLICITUD"

    if codigo.startswith("A2"):
        return "SOLICITUD SIN INFORME DE BÚSQUEDA"

    if codigo.startswith("A3"):
        return "INFORME DE BÚSQUEDA"

    if codigo.startswith("A8"):
        return "CORRECCIÓN DE SOLICITUD"

    if codigo.startswith("A9"):
        return "CORRECCIÓN DE SOLICITUD"

    if codigo.startswith("B1"):
        return "PATENTE CONCEDIDA"

    if codigo.startswith("B2"):
        return "PATENTE MODIFICADA"

    if codigo.startswith("B3"):
        return "PATENTE MODIFICADA"

    if codigo.startswith("B8"):
        return "CORRECCIÓN DE PATENTE"

    if codigo.startswith("B9"):
        return "CORRECCIÓN DE PATENTE"

    return f"PUBLICACIÓN {codigo}"


# ============================================================
# SESIÓN HTTP
# ============================================================

def crear_sesion():
    sesion = requests.Session()
    sesion.headers.update(CABECERAS)

    return sesion


def descargar_con_reintentos(
    sesion,
    url,
    intentos=3,
):
    ultimo_error = None

    for intento in range(
        1,
        intentos + 1,
    ):
        try:
            respuesta = sesion.get(
                url,
                timeout=(15, 90),
            )

            respuesta.raise_for_status()

            if not respuesta.content:
                raise RuntimeError(
                    "La EPO devolvió una "
                    "respuesta vacía."
                )

            return respuesta

        except (
            requests.RequestException,
            RuntimeError,
        ) as error:
            ultimo_error = error

            if intento < intentos:
                time.sleep(
                    intento * 2
                )

    raise RuntimeError(
        f"No se pudo descargar {url}: "
        f"{ultimo_error}"
    )


# ============================================================
# ÚLTIMA FECHA DE PUBLICACIÓN
# ============================================================

def obtener_ultima_fecha_publicacion():
    sesion = crear_sesion()

    respuesta = descargar_con_reintentos(
        sesion,
        URL_FECHAS,
    )

    sopa = BeautifulSoup(
        respuesta.text,
        "html.parser",
    )

    fechas = set()

    for enlace in sopa.find_all(
        "a",
        href=True,
    ):
        href = enlace.get(
            "href",
            "",
        )

        coincidencias = re.findall(
            r"/publication-dates/"
            r"(\d{8})(?:/|$)",
            href,
        )

        for fecha in coincidencias:
            fechas.add(fecha)

        texto = limpiar(
            enlace.get_text(
                " ",
                strip=True,
            )
        )

        coincidencia_texto = re.fullmatch(
            r"(\d{4})[/-](\d{2})[/-](\d{2})",
            texto,
        )

        if coincidencia_texto:
            fechas.add(
                "".join(
                    coincidencia_texto.groups()
                )
            )

    if not fechas:
        fechas.update(
            re.findall(
                r"\b20\d{6}\b",
                respuesta.text,
            )
        )

    if not fechas:
        raise RuntimeError(
            "No se encontró ninguna fecha "
            "de publicación en la EPO."
        )

    hoy = datetime.now(
        ZONA_HORARIA
    ).strftime("%Y%m%d")

    fechas_validas = [
        fecha
        for fecha in fechas
        if fecha <= hoy
    ]

    if not fechas_validas:
        raise RuntimeError(
            "No se encontró una fecha "
            "de publicación válida."
        )

    ultima_fecha = max(
        fechas_validas
    )

    print(
        "Última publicación EPO: "
        f"{ultima_fecha}"
    )

    return ultima_fecha


# ============================================================
# LISTADO SEMANAL
# ============================================================

def obtener_listado_semanal(fecha):
    url = (
        f"{BASE_API}/publication-dates/"
        f"{fecha}/patents"
    )

    sesion = crear_sesion()

    respuesta = descargar_con_reintentos(
        sesion,
        url,
    )

    sopa = BeautifulSoup(
        respuesta.text,
        "html.parser",
    )

    identificadores = []
    vistos = set()

    for enlace in sopa.find_all(
        "a",
        href=True,
    ):
        href = urljoin(
            respuesta.url,
            enlace.get(
                "href",
                "",
            ),
        )

        coincidencia = re.search(
            r"/patents/"
            r"([^/?#]+)$",
            href,
            flags=re.IGNORECASE,
        )

        if not coincidencia:
            continue

        identificador = limpiar(
            coincidencia.group(1)
        )

        if not identificador:
            continue

        if identificador in vistos:
            continue

        vistos.add(identificador)

        identificadores.append(
            identificador
        )

    if not identificadores:
        raise RuntimeError(
            "La EPO no devolvió publicaciones "
            f"para {fecha}."
        )

    print(
        "Publicaciones encontradas: "
        f"{len(identificadores)}"
    )

    if (
        len(identificadores)
        > MAXIMO_PUBLICACIONES_SEMANA
    ):
        print(
            "Se procesarán las primeras "
            f"{MAXIMO_PUBLICACIONES_SEMANA}."
        )

    return identificadores[
        :MAXIMO_PUBLICACIONES_SEMANA
    ]


# ============================================================
# EXTRACCIÓN DEL DOCUMENTO XML
# ============================================================

def obtener_codigo_publicacion(
    raiz,
    identificador,
):
    codigo = limpiar(
        raiz.attrib.get(
            "kind",
            "",
        )
    ).upper()

    if codigo:
        return codigo

    coincidencia = re.search(
        r"(A1|A2|A3|A8|A9|"
        r"B1|B2|B3|B8|B9)$",
        identificador,
        flags=re.IGNORECASE,
    )

    if coincidencia:
        return coincidencia.group(
            1
        ).upper()

    return ""


def obtener_numero_publicacion(
    raiz,
    identificador,
):
    numero = limpiar(
        raiz.attrib.get(
            "doc-number",
            "",
        )
    )

    if numero:
        return numero.lstrip("0") or "0"

    coincidencia = re.search(
        r"EP0*(\d+)",
        identificador,
        flags=re.IGNORECASE,
    )

    if coincidencia:
        return coincidencia.group(1)

    return identificador


def obtener_fecha_documento(
    raiz,
    fecha_semanal,
):
    fecha = limpiar(
        raiz.attrib.get(
            "date-publ",
            "",
        )
    )

    if re.fullmatch(
        r"\d{8}",
        fecha,
    ):
        return fecha

    for elemento in elementos_por_nombre(
        raiz,
        "date",
    ):
        texto = limpiar(
            "".join(
                elemento.itertext()
            )
        )

        coincidencia = re.search(
            r"\b\d{8}\b",
            texto,
        )

        if coincidencia:
            return coincidencia.group(0)

    return fecha_semanal


def obtener_titulo(raiz):
    titulos = []
    idioma_actual = ""

    for bloque in elementos_por_nombre(
        raiz,
        "B540",
    ):
        for elemento in list(bloque):
            etiqueta = nombre_local(
                elemento.tag
            ).upper()

            texto = limpiar(
                "".join(
                    elemento.itertext()
                )
            )

            if etiqueta == "B541":
                idioma_actual = texto.lower()

            elif etiqueta == "B542" and texto:
                titulos.append(
                    (
                        idioma_actual,
                        texto,
                    )
                )

    for idioma, titulo in titulos:
        if idioma == "en":
            return titulo

    if titulos:
        return titulos[0][1]

    return "Título no disponible"


def obtener_solicitantes(raiz):
    resultado = []
    vistos = set()

    for bloque in elementos_por_nombre(
        raiz,
        "B710",
    ):
        for persona in elementos_por_nombre(
            bloque,
            "B711",
        ):
            for elemento in elementos_por_nombre(
                persona,
                "snm",
            ):
                nombre = limpiar(
                    "".join(
                        elemento.itertext()
                    )
                )

                clave = nombre.casefold()

                if (
                    nombre
                    and clave not in vistos
                ):
                    vistos.add(clave)
                    resultado.append(nombre)

    return resultado[:20]


def obtener_inventores(raiz):
    resultado = []
    vistos = set()

    for bloque in elementos_por_nombre(
        raiz,
        "B720",
    ):
        for persona in elementos_por_nombre(
            bloque,
            "B721",
        ):
            for elemento in elementos_por_nombre(
                persona,
                "snm",
            ):
                nombre = limpiar(
                    "".join(
                        elemento.itertext()
                    )
                )

                clave = nombre.casefold()

                if (
                    nombre
                    and clave not in vistos
                ):
                    vistos.add(clave)
                    resultado.append(nombre)

    return resultado[:30]


def descargar_publicacion(
    identificador,
    fecha_semanal,
):
    url_xml = (
        f"{BASE_API}/patents/"
        f"{identificador}/document.xml"
    )

    sesion = crear_sesion()

    try:
        respuesta = descargar_con_reintentos(
            sesion,
            url_xml,
            intentos=3,
        )

        raiz = ET.fromstring(
            respuesta.content
        )

        codigo = obtener_codigo_publicacion(
            raiz,
            identificador,
        )

        numero = obtener_numero_publicacion(
            raiz,
            identificador,
        )

        fecha = obtener_fecha_documento(
            raiz,
            fecha_semanal,
        )

        titulo = obtener_titulo(
            raiz
        )

        solicitantes = obtener_solicitantes(
            raiz
        )

        inventores = obtener_inventores(
            raiz
        )

        if solicitantes:
            solicitante_principal = (
                solicitantes[0]
            )

        else:
            solicitante_principal = (
                "Solicitante no indicado"
            )

        tipo = clasificar_tipo(
            codigo
        )

        numero_completo = (
            f"EP{numero} {codigo}"
        ).strip()

        url_html = (
            f"{BASE_API}/patents/"
            f"{identificador}/document.html"
        )

        titulo_rss = (
            f"{tipo} | "
            f"{solicitante_principal} | "
            f"{titulo} | "
            f"{numero_completo}"
        )

        if solicitantes:
            solicitantes_html = (
                "<br>".join(
                    html.escape(nombre)
                    for nombre
                    in solicitantes
                )
            )

        else:
            solicitantes_html = (
                "No indicado"
            )

        if inventores:
            inventores_html = (
                "<br>".join(
                    html.escape(nombre)
                    for nombre
                    in inventores
                )
            )

        else:
            inventores_html = (
                "No indicados"
            )

        fecha_visible = (
            f"{fecha[6:8]}/"
            f"{fecha[4:6]}/"
            f"{fecha[0:4]}"
        )

        descripcion = (
            f"<p><strong>Tipo:</strong> "
            f"{html.escape(tipo)}</p>"

            f"<p><strong>"
            f"Solicitante/titular:"
            f"</strong><br>"
            f"{solicitantes_html}</p>"

            f"<p><strong>Inventores:"
            f"</strong><br>"
            f"{inventores_html}</p>"

            f"<p><strong>Título:</strong> "
            f"{html.escape(titulo)}</p>"

            f"<p><strong>Publicación:</strong> "
            f"{html.escape(numero_completo)}</p>"

            f"<p><strong>Fecha:</strong> "
            f"{fecha_visible}</p>"

            f'<p><a href="'
            f'{html.escape(url_html)}">'
            f"Abrir publicación oficial "
            f"en la EPO"
            f"</a></p>"
        )

        identificador_rss = hashlib.sha256(
            (
                "epo-publicacion-corregida-v2|"
                f"{identificador}"
            ).encode("utf-8")
        ).hexdigest()

        return {
            "id": identificador_rss,
            "identificador_epo": identificador,
            "titulo": titulo_rss,
            "url": url_html,
            "descripcion": descripcion,
            "fecha": fecha_iso_desde_epo(
                fecha
            ).isoformat(),
            "fecha_epo": fecha,
            "tipo": tipo,
            "codigo": codigo,
            "numero": numero_completo,
            "solicitantes": solicitantes,
            "inventores": inventores,
        }

    except Exception as error:
        print(
            f"ERROR {identificador}: "
            f"{error}"
        )

        return None


def descargar_publicaciones(
    identificadores,
    fecha,
):
    publicaciones = []
    total = len(identificadores)

    with ThreadPoolExecutor(
        max_workers=MAXIMO_TRABAJADORES
    ) as ejecutor:
        tareas = {
            ejecutor.submit(
                descargar_publicacion,
                identificador,
                fecha,
            ): identificador
            for identificador
            in identificadores
        }

        completadas = 0

        for tarea in as_completed(
            tareas
        ):
            completadas += 1

            publicacion = tarea.result()

            if publicacion:
                publicaciones.append(
                    publicacion
                )

            if (
                completadas % 25 == 0
                or completadas == total
            ):
                print(
                    "Documentos procesados: "
                    f"{completadas}/{total}"
                )

    publicaciones.sort(
        key=lambda elemento: (
            (
                elemento.get(
                    "solicitantes"
                )
                or [
                    "Solicitante no indicado"
                ]
            )[0].casefold(),
            elemento.get(
                "numero",
                "",
            ),
        )
    )

    return publicaciones


# ============================================================
# HISTORIAL
# ============================================================

def cargar_historial():
    if not ARCHIVO_HISTORIAL.exists():
        return []

    try:
        contenido = json.loads(
            ARCHIVO_HISTORIAL.read_text(
                encoding="utf-8"
            )
        )

        if isinstance(contenido, list):
            return contenido

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
    por_id = {}

    for publicacion in anteriores:
        identificador = publicacion.get(
            "identificador_epo"
        )

        if identificador:
            por_id[identificador] = (
                publicacion
            )

    for publicacion in nuevas:
        identificador = publicacion[
            "identificador_epo"
        ]

        por_id[identificador] = (
            publicacion
        )

    resultado = list(
        por_id.values()
    )

    resultado.sort(
        key=lambda elemento: elemento.get(
            "fecha",
            "",
        ),
        reverse=True,
    )

    return resultado[
        :MAXIMO_ENTRADAS_RSS
    ]


def guardar_historial(publicaciones):
    ARCHIVO_HISTORIAL.write_text(
        json.dumps(
            publicaciones,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


# ============================================================
# CREACIÓN DEL RSS
# ============================================================

def crear_rss(publicaciones):
    ET.register_namespace(
        "atom",
        "http://www.w3.org/2005/Atom",
    )

    rss = ET.Element(
        "rss",
        {
            "version": "2.0"
        },
    )

    canal = ET.SubElement(
        rss,
        "channel",
    )

    ET.SubElement(
        canal,
        "title",
    ).text = (
        "Nuevas publicaciones EPO"
    )

    ET.SubElement(
        canal,
        "link",
    ).text = URL_EPO

    ET.SubElement(
        canal,
        "description",
    ).text = (
        "Solicitudes, patentes concedidas "
        "y correcciones publicadas por la "
        "Oficina Europea de Patentes."
    )

    ET.SubElement(
        canal,
        "language",
    ).text = "es-ES"

    ET.SubElement(
        canal,
        "lastBuildDate",
    ).text = fecha_rss(
        datetime.now(
            timezone.utc
        )
    )

    ET.SubElement(
        canal,
        "ttl",
    ).text = "10080"

    ET.SubElement(
        canal,
        "{http://www.w3.org/2005/Atom}link",
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
        ).text = publicacion[
            "titulo"
        ]

        ET.SubElement(
            item,
            "link",
        ).text = publicacion[
            "url"
        ]

        ET.SubElement(
            item,
            "guid",
            {
                "isPermaLink": "false"
            },
        ).text = publicacion[
            "id"
        ]

        fecha = datetime.fromisoformat(
            publicacion["fecha"]
        )

        ET.SubElement(
            item,
            "pubDate",
        ).text = fecha_rss(
            fecha
        )

        ET.SubElement(
            item,
            "description",
        ).text = publicacion[
            "descripcion"
        ]

        ET.SubElement(
            item,
            "category",
        ).text = publicacion.get(
            "tipo",
            "EPO",
        )

        for solicitante in publicacion.get(
            "solicitantes",
            [],
        ):
            ET.SubElement(
                item,
                "category",
            ).text = solicitante

    arbol = ET.ElementTree(
        rss
    )

    ET.indent(
        arbol,
        space="  ",
    )

    arbol.write(
        ARCHIVO_RSS,
        encoding="utf-8",
        xml_declaration=True,
    )

    ET.parse(
        ARCHIVO_RSS
    )

    print(
        f"feed.xml generado: "
        f"{ARCHIVO_RSS.stat().st_size} "
        "bytes"
    )


# ============================================================
# PROGRAMA PRINCIPAL
# ============================================================

def main():
    print(
        "========================================"
    )

    print(
        "NUEVAS PUBLICACIONES EPO"
    )

    print(
        "========================================"
    )

    fecha = (
        obtener_ultima_fecha_publicacion()
    )

    identificadores = (
        obtener_listado_semanal(
            fecha
        )
    )

    nuevas = descargar_publicaciones(
        identificadores,
        fecha,
    )

    if not nuevas:
        raise RuntimeError(
            "No se pudo extraer ninguna "
            "publicación de la EPO."
        )

    anteriores = cargar_historial()

    resultado = mezclar_publicaciones(
        nuevas,
        anteriores,
    )

    guardar_historial(
        resultado
    )

    crear_rss(
        resultado
    )

    print("")

    print(
        "Proceso finalizado correctamente."
    )

    print(
        "Publicaciones semanales: "
        f"{len(nuevas)}"
    )

    print(
        "Entradas guardadas: "
        f"{len(resultado)}"
    )

    print(
        f"URL para Feedly: {URL_RSS}"
    )


if __name__ == "__main__":
    try:
        main()

    except Exception as error:
        print(
            f"ERROR: "
            f"{type(error).__name__}: "
            f"{error}",
            file=sys.stderr,
        )

        sys.exit(1)
