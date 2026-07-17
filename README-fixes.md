# Fixes al tutorial — v2

Aplica sobre `main` @ `f91364d47055049cac22b8cefd9a2af9664b3cdb` (el commit
"Se Agrega Tutorial V1" que ya tienes). Validado con `git apply --check`
contra un clone fresco de ese HEAD exacto.

Solo toca `static/tutorial.js`. No hace falta tocar `base.html` ni
`style.css` de nuevo.

```bash
git apply --check 01-tutorial-js-fixes.diff
git apply 01-tutorial-js-fixes.diff
```

## 1) "Se perdió en el paso 6, el filtro de mood se fue a cualquier lado"

**Causa:** `showStep()` es asíncrona (espera a `scrollIntoView` + un par de
`wait()`). Si tocás Siguiente/Anterior más rápido de lo que tarda esa cadena
en resolverse, quedan varias corriendo en paralelo — y la última en
*resolver* (no la última en la que hiciste clic) es la que terminaba
pintando el spotlight. El contador "Paso X de 18" ya usa una variable
compartida así que mostraba el número correcto, pero el aro dorado se movía
detrás de un elemento de un paso viejo (o quedaba a mitad de la transición
CSS entre uno y otro) — de ahí el highlight aterrizando en cualquier parte,
en tu caso sobre la barra del reproductor en vez del bloque de géneros.

**Fix:** un `renderToken` que se incrementa en cada `showStep()`. Antes de
pintar, cada cadena asíncrona revisa si sigue siendo la más reciente; si ya
hiciste otro clic mientras tanto, esa cadena vieja se descarta sin dibujar
nada. Así solo el paso realmente vigente llega a la pantalla, sin importar
qué tan rápido toques los botones.

## 2) El reproductor en primer plano no abre — pistas/artistas similares se pierden

**Causa:** `openNowPlaying()` (la función real de la app) no hace nada si
todavía no hay `window._currentTrack` — que es exactamente la situación más
común la primera vez que alguien abre la app y sigue el tutorial sin haber
tocado play todavía. El tutorial ya tenía un texto de respaldo para ese
caso, pero eso significa que casi ningún usuario nuevo llegaba a *ver* de
verdad el reproductor en primer plano ni sus botones de "similares" — se iba
directo al texto.

**Fix:** se agregó un modo demo. Si hay una pista sonando de verdad, el tour
sigue usando el reproductor real tal cual. Si no hay ninguna, el tutorial
inyecta una pista de muestra descartable (sin `id` ni `audio_url` — no hay
forma de que eso llegue a sonar) solo para que el panel se dibuje con datos
reales, abre el overlay a pantalla completa igual que lo haría
`openNowPlaying()`, y al salir de esos pasos restaura `window._currentTrack`
a lo que tenía antes. Además se agregó un paso nuevo ("Así se ve") que
muestra el reproductor recién abierto antes de entrar en los botones de
pistas/artistas similares — antes se saltaba directo del mini-player a esos
dos botones.

Con esto el tour ahora tiene **19 pasos** en vez de 18 (el contador y el
"Finalizar" del último paso ya son dinámicos — no había ningún "18"
hardcodeado en el código, solo en la documentación).
