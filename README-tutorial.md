# Tutorial de bienvenida — Orbyte

Implementa el recorrido guiado pedido en el ticket. Generado y validado contra
`main` @ `206337c2e6010285c54781870bd128631ba7b4ed` con `git apply --check`.

## Archivos

| Archivo | Contenido |
|---|---|
| `00-ALL-COMBINED.diff` | Los 3 cambios en un solo diff (aplícalo si quieres todo de una) |
| `01-base-html.diff` | `templates/base.html`: ids estables en botones M3U + `<script>` de tutorial.js |
| `02-style-css.diff` | `static/style.css`: estilos del modal, spotlight, tooltip y botón flotante |
| `03-tutorial-js-new.diff` | `static/tutorial.js` (archivo nuevo): el motor del tutorial |

## Aplicar

```bash
git clone https://github.com/orbytedirector-cmd/Orbyte.git
cd Orbyte
git apply --check 00-ALL-COMBINED.diff   # o los 3 sueltos, en cualquier orden
git apply 00-ALL-COMBINED.diff
```

Si tu working copy ya tiene commits después de `206337c`, revalida con
`git apply --check` contra un clone fresco antes de aplicar — mismo flujo que
venimos usando.

## Cómo funciona

- **Modal de bienvenida**: al cargar Home (`/`) por primera vez (`localStorage`
  vacío), aparece a los ~700ms preguntando "Comenzar recorrido" / "Ahora no".
  No vuelve a aparecer solo después de la primera vez (se guarda el estado en
  `localStorage['orbyte_tutorial_status']`), pero queda un botón flotante 🎓
  abajo a la izquierda (arriba del player bar) para reabrirlo cuando quieras.
- **Recorrido guiado**: 18 pasos con spotlight (resalta el elemento real de la
  UI con un aro dorado + fondo oscurecido) y un tooltip con Anterior/Siguiente/
  Saltar. En mobile el tooltip se fija como bottom-sheet arriba del player bar
  (pensado para iPhone); en desktop se posiciona junto al elemento resaltado.
  También responde a teclado: `←`/`→` navegan, `Esc` cierra.
- **Bloquea la interacción de fondo** mientras el tour está activo (el backdrop
  captura clicks/scroll) para que nadie navegue por accidente a mitad del
  recorrido y rompa un paso siguiente.
- **Pasos con estado dinámico** (reproductor a pantalla completa, pistas/artistas
  similares, playlist): el tutorial abre/cierra esos paneles por su cuenta
  (`openNowPlaying`, `togglePlaylist`, etc.) usando las funciones globales que
  ya expone `base.html`/`player.js`. Si no hay ninguna pista sonando, el
  reproductor a pantalla completa no puede abrirse (así está el código hoy),
  así que esos pasos degradan a un texto explicativo alternativo (`fallbackBody`)
  en vez de romperse o mostrar un spotlight sobre algo invisible.

### Los 18 pasos, en orden

1. Introducción del sistema
2. Buscador (`#search-input`)
3. Búsqueda manual por letra (`.alphabet-bar`)
4. Búsqueda avanzada (`.adv-search-open-btn`)
5. Filtros de calidad / LED del DAC (`.quality-grid`)
6. Mood / era / idioma / género (`.genre-chips`)
7. Favoritos (`.fav-banner`)
8. Letra (`#lyrics-toggle-btn`)
9. Normalizador (`#normalize-btn`)
10. Repeat (`#repeat-btn`)
11. Random (`#shuffle-btn`)
12. Reproductor en primer plano (`#player-cover-wrap`)
13. Pistas similares (`#np-similar-btn`)
14. Artistas similares (`#np-similar-artists-btn`)
15. Playlist — abrir panel (`.playlist-icon-btn`)
16. Importar M3U (`#pl-import-btn`, id nuevo)
17. Exportar M3U (`#pl-export-btn`, id nuevo)
18. Cierre / cómo reabrir el tutorial

## Nota sobre los 2 ids nuevos

`templates/base.html` — los botones de importar/exportar M3U del panel de
playlist no tenían `id` (solo `onclick`), así que les agregué
`id="pl-import-btn"` / `id="pl-export-btn"` para poder apuntarles desde el
tutorial. Es un cambio de una línea cada uno, no toca el comportamiento
existente (los `onclick` quedan intactos).
