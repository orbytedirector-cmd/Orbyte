"""Salud · «Reparar tags» de MP3 (pedido de Niko, 11-11-2026).

Algunos MP3 traen la letra (USLT) o un comentario (COMM) en UTF-16 con la marca de orden de bytes (BOM) mala: ffmpeg
avisa «Incorrect BOM value» / «Error reading lyrics, skipped» en cada decodificación y Salud los marca «con errores»,
aunque el audio está intacto. Aquí se reescriben esos cuadros en UTF-8 con el mismo texto (rescatado del cuadro
crudo: UTF-16 sin BOM, little o big endian), sin tocar el audio ni los demás tags. Si el texto no se puede rescatar,
el cuadro roto se quita (la letra sincronizada de Orbyte no viene de ahí).
"""
import os
import re
import shutil
import struct
import tempfile

ERRORES_TAG = re.compile(r'^(Incorrect BOM value|Error reading (lyrics|comment frame|frame \w+), skipped)$', re.I)
NOTAS = ('confirmado:', 'falla de lectura')


def solo_tags(errores):
    """¿Los errores de la decodificación son solo de tags (el audio está bien)?"""
    reales = [e for e in errores or [] if not str(e).startswith(NOTAS)]
    return bool(reales) and all(ERRORES_TAG.match(str(e).strip()) for e in reales)


def _sincsafe(b):
    return (b[0] << 21) | (b[1] << 14) | (b[2] << 7) | b[3]


def _cuadros_crudos(ruta):
    """{id: [bytes del cuadro]} de USLT y COMM, leídos del ID3v2 tal cual (sin el parser de mutagen)."""
    with open(ruta, 'rb') as f:
        cab = f.read(10)
        if len(cab) < 10 or cab[:3] != b'ID3':
            return {}, None
        ver, flags = cab[3], cab[5]
        datos = f.read(_sincsafe(cab[6:10]))
    if flags & 0x80 and ver < 4:                     # desincronización de todo el tag (v2.3)
        datos = datos.replace(b'\xff\x00', b'\xff')
    i = 0
    if flags & 0x40:                                 # cabecera extendida
        i = (_sincsafe(datos[:4]) if ver >= 4 else struct.unpack('>I', datos[:4])[0] + 4)
    out = {}
    while i + 10 <= len(datos):
        fid = datos[i:i + 4]
        if not re.fullmatch(rb'[A-Z0-9]{4}', fid):
            break
        tam = _sincsafe(datos[i + 4:i + 8]) if ver >= 4 else struct.unpack('>I', datos[i + 4:i + 8])[0]
        fflags = datos[i + 9]
        cuerpo = datos[i + 10:i + 10 + tam]
        if ver >= 4 and fflags & 0x02:               # desincronización del cuadro (v2.4)
            cuerpo = cuerpo.replace(b'\xff\x00', b'\xff')
        if fid in (b'USLT', b'COMM') and not (fflags & 0x0C):     # sin comprimir ni cifrar
            out.setdefault(fid.decode(), []).append(cuerpo)
        i += 10 + tam
    return out, ver


def _texto_utf16(b):
    """Texto UTF-16 con o sin BOM (el que da menos caracteres raros)."""
    if b[:2] in (b'\xff\xfe', b'\xfe\xff'):
        try:
            return b.decode('utf-16')
        except UnicodeDecodeError:
            b = b[2:]
    mejor = None
    for cod in ('utf-16-le', 'utf-16-be'):
        try:
            t = b[:len(b) // 2 * 2].decode(cod, errors='replace')
        except Exception:  # noqa: BLE001
            continue
        raros = sum(1 for c in t if c == '�' or (ord(c) < 32 and c not in '\n\r\t') or 0x3400 <= ord(c) <= 0x9fff
                    and not re.search(r'[぀-ヿ]', t))
        if mejor is None or raros < mejor[0]:
            mejor = (raros, t)
    return mejor[1] if mejor else ''


def _partir(cuerpo):
    """(lang, desc, texto) de un USLT/COMM crudo."""
    enc, lang, resto = cuerpo[0], cuerpo[1:4].decode('latin-1', 'replace'), cuerpo[4:]
    if enc in (1, 2):
        k = 0
        while k + 1 < len(resto) and resto[k:k + 2] != b'\x00\x00':
            k += 2
        desc, texto = _texto_utf16(resto[:k]), _texto_utf16(resto[k + 2:])
    else:
        k = resto.find(b'\x00')
        k = len(resto) if k < 0 else k
        cod = 'utf-8' if enc == 3 else 'latin-1'
        desc, texto = resto[:k].decode(cod, 'replace'), resto[k + 1:].decode(cod, 'replace')
    return lang, desc.strip('\x00'), texto.strip('\x00')


def reparar(ruta):
    """Reescribe la letra y los comentarios en UTF-8 (mismo texto). → {'cuadros': n, 'rescatados': n, 'quitados': n}.
    Se escribe en una copia y se reemplaza al final: si algo falla, el original queda como estaba."""
    from mutagen.id3 import COMM, ID3, USLT
    crudos, ver = _cuadros_crudos(ruta)
    out = {'cuadros': 0, 'rescatados': 0, 'quitados': 0}
    if not crudos:
        return out
    fd, tmp = tempfile.mkstemp(prefix='.tags-', suffix='.mp3', dir=os.path.dirname(ruta))
    os.close(fd)
    try:
        shutil.copy2(ruta, tmp)
        tags = ID3(tmp)
        for fid, cls in (('USLT', USLT), ('COMM', COMM)):
            if fid not in crudos:
                continue
            tags.delall(fid)
            vistos = set()
            for cuerpo in crudos[fid]:
                out['cuadros'] += 1
                try:
                    lang, desc, texto = _partir(cuerpo)
                except Exception:  # noqa: BLE001
                    out['quitados'] += 1
                    continue
                if not texto.strip() or (lang, desc) in vistos:
                    out['quitados'] += 1
                    continue
                vistos.add((lang, desc))
                lang = lang if re.fullmatch(r'[A-Za-z]{3}', lang or '') else 'XXX'
                tags.add(cls(encoding=3, lang=lang, desc=desc, text=texto))
                out['rescatados'] += 1
        tags.save(tmp, v2_version=4 if (ver or 3) >= 4 else 3)
        os.chmod(tmp, os.stat(ruta).st_mode & 0o7777)
        os.replace(tmp, ruta)
        tmp = None
    finally:
        if tmp and os.path.exists(tmp):
            os.remove(tmp)
    return out
