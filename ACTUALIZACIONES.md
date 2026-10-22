# Actualizaciones automáticas de Orbyte — procedimiento

Cómo publicar una versión nueva para que les llegue sola a los usuarios, plataforma por plataforma.

## Cómo funciona (en una línea)

El servidor guarda la última versión de cada app en `~/Documentos/Orbyte-login/apk/`. Cada app, al abrirse (o con **Buscar actualización**), compara su versión con la publicada. Si la publicada es **mayor**, avisa y la instala.

| Plataforma | De dónde sale la versión | Cómo se instala en el usuario |
|---|---|---|
| Android | Se compila en el servidor | La app la descarga y abre el instalador de Android |
| iPhone | GitHub Actions → release `ipa-main` | La app abre SideStore con la descarga |
| Windows | GitHub Actions → release `dev-windows` (Setup) | Instalación silenciosa: se cierra y se vuelve a abrir sola |
| Linux | GitHub Actions → release `dev-linux` (AppImage) | Reemplaza el AppImage; **Reiniciar ahora** |

## Rutas que se usan

| Qué | Dónde |
|---|---|
| Servidor de Orbyte | `~/Documentos/Orbyte-login` |
| Proyecto Android | `~/dev/Orbyte-Android-Root/Orbyte-Android` |
| Descargas de GitHub (IPA, Setup, AppImage) | `~/orbyte-artifact/` |
| Versiones publicadas | `~/Documentos/Orbyte-login/apk/` (fuera de git) |

## 0. Antes de publicar: servidor al día

Solo si hubo cambios en el servidor (`git pull` trae algo):

```
cd ~/Documentos/Orbyte-login && git pull && sudo systemctl restart orbyte.service
```

El servidor ya no se reinicia solo con un `git pull`: los cambios se aplican recién con `systemctl restart`.

---

## 1. Android

**Requisito único:** compilar siempre en el servidor (la firma sale de `~/.android/debug.keystore`; si cambia la llave, Android rechaza la actualización).

1. Compilar:
```
cd ~/dev/Orbyte-Android-Root/Orbyte-Android && git pull && ./gradlew :app:assembleDebug
```
2. Publicar:
```
cd ~/Documentos/Orbyte-login && python3 tools/publicar_apk.py ~/dev/Orbyte-Android-Root/Orbyte-Android/app/build/outputs/apk/debug/app-debug.apk "Qué trae esta versión"
```
3. Comprobar:
```
python3 tools/publicar_apk.py --estado
```

**Número de versión:** sube solo con cada commit (`0.2.<commits>`). Si no hubo cambios en el código, el APK sale con el mismo número y el script se niega a publicarlo.

**En el teléfono:** al abrir Orbyte aparece *"Nueva versión de Orbyte"* → **Actualizar** → (la primera vez) permitir instalar apps de esta fuente → **Continuar** → **Instalar**. También: Perfil → Aplicación → **Buscar actualización**.

**Respaldo de la llave de firma** (una vez, y guardarla fuera del servidor, nunca en GitHub):
```
mkdir -p ~/Documentos/respaldos && cp ~/.android/debug.keystore ~/Documentos/respaldos/orbyte-debug.keystore
```

---

## 2. iPhone

1. Compilar en GitHub: repo **Orbyte-iOS** → **Actions** → **Build Orbyte (unsigned IPA)** → **Run workflow** → rama **main**.
   - Tarda ~12 min. Si GitHub la cancela con *"not acquired by Runner… capacity constraints"*, no es un error del código: reintentar más tarde.
2. Descargar `Orbyte.ipa` de la release **ipa-main** (dice *"Build N"*) a `~/orbyte-artifact/`.
3. Publicar:
```
cd ~/Documentos/Orbyte-login && python3 tools/publicar_ipa.py ~/orbyte-artifact/Orbyte.ipa "Qué trae esta versión"
```
4. Comprobar (muestra también los enlaces de instalación):
```
python3 tools/publicar_ipa.py --estado
```

**Número de versión:** el build es el número de corrida de GitHub (`1.0 (build N)`), sube solo en cada compilación.

**En el iPhone:** con **wifi** y **LocalDevVPN encendida** (SideStore la necesita para instalar), abrir Orbyte → *"Nueva versión de Orbyte"* → **Actualizar** → se abre SideStore → confirmar. También: Perfil → Aplicación → **Buscar actualización**.

- Funciona desde cualquier wifi (casa o fuera), **sin Tailscale**: el IPA se descarga por Funnel.
- Con datos móviles no: SideStore exige wifi.
- Enlace manual (si el aviso falla): copiar el enlace **Internet** de `--estado` y abrirlo en Safari.

**Requisitos ya configurados (no tocar):**
- Funnel con la ruta `/descargas` → `tailscale funnel status` debe listarla.
- En `~/Documentos/Orbyte-login/.env`: `ORBYTE_URL_PUBLICA=https://serverkillerlinux.tail164cca.ts.net`

---

## 3. Orbyte Desktop — Windows

1. Compilar en GitHub: repo **Orbyte-Desktop** → **Actions** → **Build Windows** → **Run workflow** → **main**.
2. Descargar `Orbyte-Setup-1.0.0-dev.N.exe` de la release **dev-windows** a `~/orbyte-artifact/`.
3. Publicar:
```
cd ~/Documentos/Orbyte-login && python3 tools/publicar_desktop.py ~/orbyte-artifact/Orbyte-Setup-1.0.0-dev.N.exe "Qué trae esta versión"
```

**En el PC:** al iniciar sesión aparece el aviso (o Configuración → Aplicación → **Buscar actualización**) → **Actualizar** → descarga → Orbyte se cierra, se instala sola (sin pedir administrador) y se vuelve a abrir.

- Solo se actualiza sola la versión **instalada con el Setup**.
- La **portable (ZIP)** solo avisa: bajar el ZIP nuevo y reemplazar la carpeta (la carpeta `OrbyteData` se conserva).

## 4. Orbyte Desktop — Linux

1. Compilar en GitHub: repo **Orbyte-Desktop** → **Actions** → **Build Linux** → **Run workflow** → **main**.
2. Descargar `Orbyte-1.0.0-dev.N-x86_64.AppImage` de la release **dev-linux** a `~/orbyte-artifact/`.
3. Publicar:
```
cd ~/Documentos/Orbyte-login && python3 tools/publicar_desktop.py ~/orbyte-artifact/Orbyte-1.0.0-dev.N-x86_64.AppImage "Qué trae esta versión"
```

**En el equipo:** aviso al iniciar sesión (o **Buscar actualización**) → **Actualizar** → descarga → **Reiniciar ahora**.

- Solo se actualiza solo el **AppImage**. El **.tar.gz** solo avisa: reemplazar la carpeta a mano.
- En Arch/CachyOS el AppImage necesita `fuse2` (`sudo pacman -S fuse2`).

**Números de versión:** `1.0.0-dev.N`, donde N es el número de corrida de **cada** workflow. Windows y Linux llevan numeraciones separadas (por ejemplo dev.28 en Windows y dev.26 en Linux), y cada una se compara solo contra la de su sistema.

```
python3 tools/publicar_desktop.py --estado      # lo publicado de Windows y de Linux
```

---

## 5. Primera vez con un usuario nuevo

Las versiones anteriores al actualizador no lo traen. Cada usuario instala **a mano una sola vez** una versión que ya lo tenga:

| Plataforma | Desde |
|---|---|
| Android | 0.2.145 |
| iPhone | build 112 |
| Windows | 1.0.0-dev.28 (Setup) |
| Linux | 1.0.0-dev.25 (AppImage) |

De ahí en adelante les llega sola.

## 6. Ver qué versión tiene cada uno

- **Android / iPhone:** Perfil → Aplicación.
- **Desktop:** Configuración → Aplicación.
- **Android** (también): Ajustes del teléfono → Aplicaciones → Orbyte → Versión.

## 7. Si algo no anda

| Síntoma | Causa / qué hacer |
|---|---|
| El script dice *"ya está publicada la …"* | El archivo no es más nuevo que el publicado. Compilar/bajar uno nuevo. `--forzar` solo si de verdad se quiere republicar. |
| No aparece el aviso | La versión instalada es igual o mayor que la publicada (revisar en Perfil/Configuración). El aviso se revisa al **abrir**: cerrar la app del todo y abrirla, o usar **Buscar actualización**. |
| Android: *"conflicto con un paquete existente"* | El APK se firmó con otra llave (se compiló en otro equipo). Compilar en el servidor o copiar el `debug.keystore` respaldado. |
| iPhone: SideStore se queda en "Instalar" | Falta **LocalDevVPN** encendida. |
| iPhone: *"No wifi interface satisfied"* | SideStore exige estar en un wifi (cualquiera). |
| iPhone fuera de casa no descarga | Revisar `tailscale funnel status` (debe estar `/descargas`) y `ORBYTE_URL_PUBLICA` en `.env`. |
| Usuarios externos no conectan a Orbyte | Revisar que el servidor siga en Tailscale: `tailscale status | head -3`. Si dice *Logged out*: `sudo tailscale up` y en login.tailscale.com → Machines → serverkillerlinux → **Disable key expiry**. |

**Registros útiles** (en el servidor):
```
grep "actualizador ios" ~/Documentos/Orbyte-login/logs/orbyte.log | tail -5   # descargas de iPhone (127.0.0.1 = por Funnel/internet)
```

## Resumen de comandos

```
cd ~/Documentos/Orbyte-login
python3 tools/publicar_apk.py      <apk>      "notas"   # Android
python3 tools/publicar_ipa.py      <ipa>      "notas"   # iPhone
python3 tools/publicar_desktop.py  <exe|AppImage> "notas"   # Windows / Linux
python3 tools/publicar_apk.py --estado
python3 tools/publicar_ipa.py --estado
python3 tools/publicar_desktop.py --estado
```
